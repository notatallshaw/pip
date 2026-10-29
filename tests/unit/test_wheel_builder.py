from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from pip._internal import wheel_builder
from pip._internal.exceptions import UnsupportedWheel
from pip._internal.models.link import Link
from pip._internal.req.constructors import install_req_from_line
from pip._internal.req.req_install import InstallRequirement
from pip._internal.utils.urls import path_to_url
from pip._internal.vcs.git import Git

from tests.lib import create_test_package, make_wheel


@pytest.mark.parametrize(
    "s, expected",
    [
        # Trivial.
        ("pip-18.0", True),
        # Ambiguous.
        ("foo-2-2", True),
        ("im-valid", True),
        # Invalid.
        ("invalid", False),
        ("im_invalid", False),
    ],
)
def test_contains_egg_info(s: str, expected: bool) -> None:
    result = wheel_builder._contains_egg_info(s)
    assert result == expected


@dataclass
class ReqMock:
    name: str = "pendulum"
    is_wheel: bool = False
    editable: bool = False
    link: Link | None = None
    constraint: bool = False
    source_dir: str | None = "/tmp/pip-install-123/pendulum"
    supports_pyproject_editable: bool = False


@pytest.mark.parametrize(
    "req, expected",
    [
        (ReqMock(editable=True), False),
        (ReqMock(source_dir=None), False),
        (ReqMock(link=Link("git+https://g.c/org/repo")), False),
        (ReqMock(link=Link("https://g.c/dist.tgz")), False),
        (ReqMock(link=Link("https://g.c/dist-2.0.4.tgz")), True),
    ],
)
def test_should_cache(req: ReqMock, expected: bool) -> None:
    assert wheel_builder._should_cache(cast(InstallRequirement, req)) is expected


def test_should_cache_git_sha(tmpdir: Path) -> None:
    repo_path = os.fspath(create_test_package(tmpdir, name="mypkg"))
    commit = Git.get_revision(repo_path)

    # a link referencing a sha should be cached
    url = "git+https://g.c/o/r@" + commit + "#egg=mypkg"
    req = ReqMock(link=Link(url), source_dir=repo_path)
    assert wheel_builder._should_cache(cast(InstallRequirement, req))

    # a link not referencing a sha should not be cached
    url = "git+https://g.c/o/r@master#egg=mypkg"
    req = ReqMock(link=Link(url), source_dir=repo_path)
    assert not wheel_builder._should_cache(cast(InstallRequirement, req))


@pytest.mark.parametrize("name", ["package", "my-package"])
def test_should_not_cache_local_directory(tmpdir: Path, name: str) -> None:
    project_dir = tmpdir / name
    project_dir.mkdir()
    req = ReqMock(link=Link(path_to_url(str(project_dir))))
    assert not wheel_builder._should_cache(cast(InstallRequirement, req))


@pytest.mark.parametrize(
    "field, value",
    [
        ("Name", "other"),
        ("Version", "2.0"),
        ("Requires-Python", ">=3.10"),
        ("Requires-Dist", "dependency==1.0"),
        ("Provides-Extra", "feature"),
    ],
)
def test_built_wheel_preserves_static_sdist_metadata(field: str, value: str) -> None:
    source_dist = make_wheel("package", "1.0").as_distribution("package")
    name = value if field == "Name" else "package"
    version = value if field == "Version" else "1.0"
    updates: dict[str, str | list[str]] = (
        {} if field in {"Name", "Version"} else {field: value}
    )
    wheel_dist = make_wheel(name, version, metadata_updates=updates).as_distribution(
        name
    )

    with pytest.raises(UnsupportedWheel, match=f"different {field}"):
        wheel_builder._check_static_sdist_metadata(source_dist, wheel_dist)


def test_built_wheel_can_fill_dynamic_fields() -> None:
    source_dist = make_wheel(
        "package",
        "1.0",
        metadata_updates={
            "Dynamic": ["Requires-Dist", "Requires-Python", "Provides-Extra"]
        },
    ).as_distribution("package")
    wheel_dist = make_wheel(
        "package",
        "1.0",
        metadata_updates={
            "Requires-Dist": "dependency==1.0",
            "Requires-Python": ">=3.10",
            "Provides-Extra": "feature",
        },
    ).as_distribution("package")

    wheel_builder._check_static_sdist_metadata(source_dist, wheel_dist)


def test_built_wheel_validates_newly_available_dynamic_dependencies() -> None:
    source_dist = make_wheel(
        "package", "1.0", metadata_updates={"Dynamic": "Requires-Dist"}
    ).as_distribution("package")
    wheel_dist = make_wheel(
        "package", "1.0", metadata_updates={"Requires-Dist": "dependency>=broken"}
    ).as_distribution("package")

    with pytest.raises(UnsupportedWheel, match="Invalid Requires-Dist"):
        wheel_builder._check_static_sdist_metadata(source_dist, wheel_dist)


@pytest.mark.parametrize("field", ["Requires-Dist", "Provides-Extra"])
@pytest.mark.parametrize("metadata_version", ["2.5", "2.6"])
@pytest.mark.parametrize("retains_source_value", [False, True])
def test_built_wheel_preserves_append_only_dynamic_fields(
    field: str, metadata_version: str, retains_source_value: bool
) -> None:
    source_value = "dependency==1.0" if field == "Requires-Dist" else "feature"
    added_value = "another==1.0" if field == "Requires-Dist" else "another"
    source_dist = make_wheel(
        "package",
        "1.0",
        metadata_updates={
            "Metadata-Version": metadata_version,
            "Dynamic": field,
            field: source_value,
        },
    ).as_distribution("package")
    values = [source_value, added_value] if retains_source_value else [added_value]
    wheel_dist = make_wheel(
        "package", "1.0", metadata_updates={field: values}
    ).as_distribution("package")

    if metadata_version == "2.6" and not retains_source_value:
        with pytest.raises(UnsupportedWheel, match=f"dropped a fixed {field}"):
            wheel_builder._check_static_sdist_metadata(source_dist, wheel_dist)
    else:
        wheel_builder._check_static_sdist_metadata(source_dist, wheel_dist)


@pytest.mark.parametrize("verify", [True, False])
def test_static_sdist_mismatch_removes_cached_wheel(
    tmpdir: Path, monkeypatch: pytest.MonkeyPatch, verify: bool
) -> None:
    req = install_req_from_line("package==1.0")
    req.static_sdist_metadata = make_wheel("package", "1.0").as_distribution("package")
    wheel_path = make_wheel(
        "package", "1.0", metadata_updates={"Requires-Dist": "dependency==1.0"}
    ).save_to_dir(tmpdir)
    monkeypatch.setattr(
        wheel_builder, "_build_one_inside_env", lambda *args: wheel_path
    )

    assert wheel_builder._build_one(req, str(tmpdir), verify, False) is None
    assert not Path(wheel_path).exists()


def test_static_sdist_uses_built_wheel_metadata(
    tmpdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    req = install_req_from_line("package==1.0")
    req.static_sdist_metadata = make_wheel(
        "package", "1.0", metadata_updates={"Dynamic": "Summary"}
    ).as_distribution("package")
    req.set_dist(req.static_sdist_metadata)
    assert req.metadata.get("Summary") is None
    wheel_path = make_wheel(
        "package", "1.0", metadata_updates={"Summary": "Built summary"}
    ).save_to_dir(tmpdir)
    monkeypatch.setattr(
        wheel_builder, "_build_one_inside_env", lambda *args: wheel_path
    )

    assert wheel_builder._build_one(req, str(tmpdir), True, False) == wheel_path
    assert req.get_dist().metadata["Summary"] == "Built summary"
    assert req.metadata["Summary"] == "Built summary"


@pytest.mark.parametrize(
    "metadata",
    [
        "",
        "Metadata-Version: 2.2\nName: package\n\n",
        "Metadata-Version: 2.2\nVersion: 1.0\n\n",
        "Metadata-Version: 2.2\nName: package\nVersion: broken\n\n",
        "Metadata-Version: 2.2\nName: invalid name\nVersion: 1.0\n\n",
        "Metadata-Version: 2.2\nName: package\nVersion: 1.0\n"
        "Requires-Python: >=broken\n\n",
    ],
)
def test_static_sdist_rejects_malformed_built_metadata(
    tmpdir: Path, monkeypatch: pytest.MonkeyPatch, metadata: str
) -> None:
    req = install_req_from_line("package==1.0")
    req.static_sdist_metadata = make_wheel("package", "1.0").as_distribution("package")
    wheel_path = make_wheel("package", "1.0", metadata=metadata).save_to_dir(tmpdir)
    monkeypatch.setattr(
        wheel_builder, "_build_one_inside_env", lambda *args: wheel_path
    )

    assert wheel_builder._build_one(req, str(tmpdir), False, False) is None
    assert not Path(wheel_path).exists()


def test_static_sdist_rejects_corrupt_built_archive(
    tmpdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    req = install_req_from_line("package==1.0")
    req.static_sdist_metadata = make_wheel("package", "1.0").as_distribution("package")
    wheel_path = tmpdir / "package-1.0-py3-none-any.whl"
    wheel_path.write_bytes(b"not a zip file")
    monkeypatch.setattr(
        wheel_builder, "_build_one_inside_env", lambda *args: str(wheel_path)
    )

    assert wheel_builder._build_one(req, str(tmpdir), False, False) is None
    assert not wheel_path.exists()


def test_static_sdist_removes_wheel_with_inconsistent_filename(
    tmpdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    req = install_req_from_line("package==1.0")
    req.static_sdist_metadata = make_wheel("package", "1.0").as_distribution("package")
    wheel_path = tmpdir / "package-2.0-py2.py3-none-any.whl"
    make_wheel("package", "1.0").save_to(wheel_path)
    monkeypatch.setattr(
        wheel_builder, "_build_one_inside_env", lambda *args: str(wheel_path)
    )

    assert wheel_builder._build_one(req, str(tmpdir), True, False) is None
    assert not wheel_path.exists()
