"""Keep dependency URLs fixed by input constraints in catalogue resolution."""

import hashlib
import json
from pathlib import Path

import pytest

from tests.functional.test_nab_preparation_reuse import tracked_install
from tests.lib import (
    PipTestEnvironment,
    create_basic_sdist_for_package,
    create_basic_wheel_for_package,
)
from tests.lib.wheel import make_wheel


@pytest.mark.parametrize("with_extra", [False, True])
@pytest.mark.parametrize("equivalent_url", [False, True])
def test_dependency_url_fixed_by_a_constraint_uses_the_catalogue(
    script: PipTestEnvironment, with_extra: bool, equivalent_url: bool
) -> None:
    direct = script.scratch_path / "direct"
    direct.mkdir()
    dep = make_wheel(
        "dep",
        "1",
        metadata=(
            "Metadata-Version: 2.2\nName: dep\nVersion: 1\nProvides-Extra: feature\n"
            'Requires-Dist: child==1; extra == "feature"\n'
        ),
    )
    supplied = Path(dep.save_to_dir(direct))
    url = supplied.as_uri()
    declaration = url + "#egg=dep" if equivalent_url else url
    name = "dep[feature]" if with_extra else "dep"
    make_wheel(
        "app",
        "1",
        metadata=(
            "Metadata-Version: 2.2\nName: app\nVersion: 1\n"
            f"Requires-Dist: {name} @ {declaration}\n"
        ),
    ).save_to_dir(script.scratch_path)
    create_basic_wheel_for_package(script, "child", "1")
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"dep @ {url}\n")
    report = script.scratch_path / "report.json"

    result = tracked_install(
        script,
        "app",
        options=("-c", str(constraints), "--report", str(report)),
        ignore_installed=False,
    )

    assert "NATIVE True" not in result.stdout
    assert "NATIVE False" not in result.stdout
    script.assert_installed(app="1", dep="1")
    if with_extra:
        script.assert_installed(child="1")
    else:
        script.assert_not_installed("child")
    script.pip("check")

    items = {
        item["metadata"]["name"]: item
        for item in json.loads(report.read_text())["install"]
    }
    assert items["app"]["requested"] is True
    assert items["dep"]["requested"] is False
    assert items["dep"]["is_direct"] is True
    origin = script.site_packages_path / "dep-1.dist-info" / "direct_url.json"
    assert json.loads(origin.read_text())["url"] == url


@pytest.mark.parametrize("inactive", ["marker", "extra", "no_deps", "unused"])
def test_inactive_constrained_source_is_not_prepared(
    script: PipTestEnvironment, inactive: str
) -> None:
    missing = script.scratch_path / "absent" / "dep-1-py3-none-any.whl"
    marker = ' ; python_version < "2"' if inactive == "marker" else ""
    if inactive == "extra":
        marker = ' ; extra == "feature"'
    dependency = f"Requires-Dist: dep @ {missing.as_uri()}{marker}\n"
    if inactive == "unused":
        dependency = ""
    make_wheel(
        "app",
        "1",
        metadata=(
            "Metadata-Version: 2.2\nName: app\nVersion: 1\nProvides-Extra: feature\n"
            + dependency
        ),
    ).save_to_dir(script.scratch_path)
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"dep @ {missing.as_uri()}\n")
    options: tuple[str, ...] = ("-c", str(constraints))
    if inactive == "no_deps":
        options += ("--no-deps",)

    result = tracked_install(script, "app", options=options)

    assert "NATIVE True" not in result.stdout
    assert "NATIVE False" not in result.stdout
    script.assert_installed(app="1")
    script.assert_not_installed("dep")


@pytest.mark.parametrize("source_exists", [False, True])
@pytest.mark.parametrize("with_extra", [False, True])
def test_conflicting_parent_url_cannot_replace_the_constrained_source(
    script: PipTestEnvironment, *, source_exists: bool, with_extra: bool
) -> None:
    sources = []
    for name in ["permitted", "other"]:
        folder = script.scratch_path / name
        folder.mkdir()
        sources.append(Path(make_wheel("dep", "1").save_to_dir(folder)))
    permitted, other = sources
    if not source_exists:
        other.unlink()
    name = "dep[feature]" if with_extra else "dep"
    create_basic_wheel_for_package(script, "app", "1", depends=["dep>=1"])
    make_wheel(
        "app",
        "2",
        metadata=(
            "Metadata-Version: 2.2\nName: app\nVersion: 2\n"
            f"Requires-Dist: {name} @ {other.as_uri()}\n"
        ),
    ).save_to_dir(script.scratch_path)
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"dep @ {permitted.as_uri()}\n")
    report = script.scratch_path / "report.json"

    result = tracked_install(
        script,
        "app",
        options=("-c", str(constraints), "--report", str(report)),
    )

    assert "NATIVE True" not in result.stdout
    assert "NATIVE False" not in result.stdout
    script.assert_installed(app="1", dep="1")
    script.pip("check")
    items = {
        item["metadata"]["name"]: item
        for item in json.loads(report.read_text())["install"]
    }
    assert items["dep"]["download_info"]["url"] == permitted.as_uri()


@pytest.mark.parametrize("source", ["dependency", "named", "wheel", "sdist"])
@pytest.mark.parametrize("correct_hash", [False, True])
def test_url_constraint_hashes_are_checked_before_installation(
    script: PipTestEnvironment, source: str, correct_hash: bool
) -> None:
    direct = script.scratch_path / "direct"
    direct.mkdir()
    if source == "sdist":
        supplied = create_basic_sdist_for_package(script, "dep", "1")
        supplied = supplied.rename(direct / supplied.name)
    else:
        supplied = Path(make_wheel("dep", "1").save_to_dir(direct))
    digest = hashlib.sha256(supplied.read_bytes()).hexdigest()
    wanted = digest if correct_hash else "0" * 64
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"dep @ {supplied.as_uri()} --hash=sha256:{wanted}\n")
    requirements = script.scratch_path / "requirements.txt"
    root = supplied.as_uri()
    if source == "named":
        root = f"dep @ {root}"
    elif source == "dependency":
        app = Path(
            make_wheel(
                "app",
                "1",
                metadata=(
                    "Metadata-Version: 2.2\nName: app\nVersion: 1\n"
                    f"Requires-Dist: dep @ {supplied.as_uri()}\n"
                ),
            ).save_to_dir(script.scratch_path)
        )
        root = f"app==1 --hash=sha256:{hashlib.sha256(app.read_bytes()).hexdigest()}"
    requirements.write_text(root + "\n")

    result = tracked_install(
        script,
        "-r",
        str(requirements),
        options=("-c", str(constraints), "--no-build-isolation"),
        expect_error=not correct_hash,
    )

    assert "NATIVE True" not in result.stdout
    assert "NATIVE False" not in result.stdout
    if correct_hash:
        script.assert_installed(dep="1")
        script.pip("check")
    else:
        assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr
        script.assert_not_installed("app", "dep")


def test_repeated_url_roots_cannot_reuse_a_candidate_with_conflicting_hashes(
    script: PipTestEnvironment,
) -> None:
    supplied = Path(make_wheel("dep", "1").save_to_dir(script.scratch_path))
    digest = hashlib.sha256(supplied.read_bytes()).hexdigest()
    requirements = script.scratch_path / "requirements.txt"
    requirements.write_text(
        f"dep @ {supplied.as_uri()} --hash=sha256:{digest}\n"
        f"dep @ {supplied.as_uri()} --hash=sha256:{'0' * 64}\n"
    )

    result = tracked_install(script, "-r", str(requirements), expect_error=True)

    assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr
    script.assert_not_installed("dep")


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("algorithm", ["sha256", "sha512"])
@pytest.mark.parametrize("repeat", [False, True])
def test_source_hash_intersection_cannot_be_dropped(
    script: PipTestEnvironment, named: bool, algorithm: str, repeat: bool
) -> None:
    supplied = create_basic_sdist_for_package(script, "dep", "1")
    digest = hashlib.sha256(supplied.read_bytes()).hexdigest()
    constraint_digest = (
        hashlib.sha512(supplied.read_bytes()).hexdigest()
        if algorithm == "sha512"
        else "0" * 64
    )
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(
        f"dep @ {supplied.as_uri()} --hash={algorithm}:{constraint_digest}\n"
    )
    root = f"dep @ {supplied.as_uri()}" if named else supplied.as_uri()
    declaration = f"{root} --hash=sha256:{digest}\n"
    requirements = script.scratch_path / "requirements.txt"
    requirements.write_text(declaration * (2 if repeat else 1))

    result = tracked_install(
        script,
        "-r",
        str(requirements),
        options=("-c", str(constraints), "--no-build-isolation"),
        expect_error=True,
    )

    assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr
    script.assert_not_installed("dep")


def test_constraint_hash_checks_the_source_of_a_cached_built_wheel(
    script: PipTestEnvironment,
) -> None:
    supplied = create_basic_sdist_for_package(script, "dep", "1")
    cache = script.scratch_path / "cache"
    script.pip(
        "wheel",
        "--no-index",
        "--no-deps",
        "--no-build-isolation",
        "--cache-dir",
        str(cache),
        str(supplied),
    )
    assert list(cache.rglob("dep-1-*.whl"))
    digest = hashlib.sha256(supplied.read_bytes()).hexdigest()
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"dep @ {supplied.as_uri()} --hash=sha256:{digest}\n")

    result = tracked_install(
        script,
        supplied.as_uri(),
        options=(
            "-c",
            str(constraints),
            "--cache-dir",
            str(cache),
            "--no-build-isolation",
        ),
    )

    assert "NATIVE True" not in result.stdout
    assert "NATIVE False" not in result.stdout
    script.assert_installed(dep="1")
    script.pip("check")


def test_unconstrained_url_does_not_discard_a_working_newer_parent(
    script: PipTestEnvironment,
) -> None:
    direct = script.scratch_path / "direct"
    direct.mkdir()
    supplied = Path(make_wheel("dep", "3").save_to_dir(direct))
    create_basic_wheel_for_package(script, "dep", "1")
    create_basic_wheel_for_package(script, "app", "1", depends=["dep==1"])
    make_wheel(
        "app",
        "2",
        metadata=(
            "Metadata-Version: 2.2\nName: app\nVersion: 2\n"
            f"Requires-Dist: dep @ {supplied.as_uri()}\n"
        ),
    ).save_to_dir(script.scratch_path)

    result = tracked_install(script, "app")

    assert "NATIVE True" in result.stdout
    script.assert_installed(app="2", dep="3")
    script.pip("check")


@pytest.mark.parametrize(
    "source_kind",
    ["directory", "editable", pytest.param("git", marks=pytest.mark.git)],
)
def test_no_require_hashes_allows_an_unnamed_source_without_an_archive(
    script: PipTestEnvironment, source_kind: str
) -> None:
    source = script.scratch_path / "source"
    source.mkdir()
    (source / "setup.py").write_text(
        "from setuptools import setup\n"
        "setup(name='dep', version='1', py_modules=['dep'])\n"
    )
    (source / "dep.py").write_text("")
    url = source.as_uri()
    if source_kind == "git":
        script.run("git", "init", "-b", "main", cwd=source)
        script.run("git", "add", ".", cwd=source)
        script.run("git", "commit", "-q", "-m", "Create source fixture", cwd=source)
        url = "git+" + url
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"dep @ {url} --hash=sha256:{'1' * 64}\n")
    requirements = script.scratch_path / "requirements.txt"
    root = "-e " + url if source_kind == "editable" else url
    requirements.write_text(
        f"{root} --hash=sha256:{'1' * 64} --hash=sha256:{'2' * 64}\n"
    )

    tracked_install(
        script,
        "-r",
        str(requirements),
        options=("-c", str(constraints), "--no-require-hashes", "--no-build-isolation"),
    )

    script.assert_installed(dep="1")
    script.pip("check")
