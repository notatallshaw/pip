import hashlib
import json
from itertools import repeat
from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from pip._vendor.packaging.utils import canonicalize_name

from tests.functional.test_nab_preparation_reuse import (
    PREPARATION_PROBE,
    preparation_count,
    tracked_install,
)
from tests.lib import PipTestEnvironment, create_basic_wheel_for_package
from tests.lib.server import MockServer
from tests.lib.wheel import make_wheel

if TYPE_CHECKING:
    from _typeshed.wsgi import StartResponse, WSGIEnvironment


def test_url_from_rejected_parent_does_not_replace_installed_candidate(
    script: PipTestEnvironment,
) -> None:
    wheel = create_basic_wheel_for_package(script, "dep", "1.0")
    script.pip("install", "--no-index", wheel)
    source = script.scratch_path / "direct"
    source.mkdir()
    replacement = create_basic_wheel_for_package(
        script, "dep", "1.0", depends=["unavailable==1.0"]
    )
    replacement = replacement.rename(source / replacement.name)
    create_basic_wheel_for_package(script, "app", "1.0", depends=["dep==1.0"])
    make_wheel(
        name="app",
        version="2.0",
        metadata=dedent(f"""\
            Metadata-Version: 2.1
            Name: app
            Version: 2.0
            Requires-Dist: dep @ {replacement.as_uri()}
            """),
    ).save_to(script.scratch_path / "app-2.0-py2.py3-none-any.whl")

    script.pip(
        "install", "--no-index", "--find-links", script.scratch_path, "dep", "app"
    )

    script.assert_installed(app="1.0", dep="1.0")


def test_urls_from_alternative_parent_versions_are_independent(
    script: PipTestEnvironment,
) -> None:
    sources = []
    for name, dependencies in (("refused", ["unavailable==1.0"]), ("usable", [])):
        source = script.scratch_path / name
        source.mkdir()
        wheel = create_basic_wheel_for_package(
            script, "dep", "1.0", depends=dependencies
        )
        sources.append(wheel.rename(source / wheel.name))
    for version, wheel in zip(("2.0", "1.0"), sources):
        make_wheel(
            name="app",
            version=version,
            metadata=dedent(f"""\
                Metadata-Version: 2.1
                Name: app
                Version: {version}
                Requires-Dist: dep @ {wheel.as_uri()}
                """),
        ).save_to(script.scratch_path / f"app-{version}-py2.py3-none-any.whl")

    script.pip("install", "--no-index", "--find-links", script.scratch_path, "app")

    script.assert_installed(app="1.0", dep="1.0")


def source_wheel(
    directory: Path,
    name: str,
    version: str,
    dependencies: tuple[str, ...] = (),
    *,
    feature: bool = False,
) -> Path:
    # Raw headers keep long dependency URLs on one line.
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    if feature:
        metadata += "Provides-Extra: feature\n"
    metadata += "".join(f"Requires-Dist: {dependency}\n" for dependency in dependencies)
    path = directory / f"{name}-{version}-py3-none-any.whl"
    make_wheel(name, version, metadata=metadata).save_to(path)
    return path


@pytest.mark.parametrize("shape", ["extras", "url-cycle", "indexed-parent"])
@pytest.mark.parametrize("upfront", [False, True], ids=["unrooted", "upfront"])
def test_rejected_parent_sources_need_rooted_support(
    script: PipTestEnvironment, shape: str, upfront: bool
) -> None:
    hidden = script.scratch_path / "hidden"
    hidden.mkdir()
    name, version = ("dep", "3") if shape == "extras" else ("left", "2")
    source = hidden / f"{name}-{version}-py3-none-any.whl"
    expected: dict[str, Path] = {}

    if shape == "extras":
        source_wheel(hidden, name, version, feature=True)
        surviving_dependency = "dep[feature]>=2"
    else:
        peer_directory = hidden if shape == "url-cycle" else script.scratch_path
        peer = source_wheel(
            peer_directory, "right", "2", (f"left @ {source.as_uri()}",)
        )
        dependency = f"right @ {peer.as_uri()}" if shape == "url-cycle" else "right"
        source_wheel(hidden, name, version, (dependency,))
        expected["right"] = peer
        surviving_dependency = "left>=2"

    # App2 must reveal the source before its missing dependency excludes it.
    source_wheel(script.scratch_path, name, "1")
    rejected = source_wheel(
        script.scratch_path, "app", "2", (f"{name} @ {source.as_uri()}", "missing==1")
    )
    retained = source_wheel(script.scratch_path, "app", "1", (surviving_dependency,))
    report = script.scratch_path / "source-report.json"
    roots = ("app", source) if upfront else ("app",)

    result = tracked_install(
        script, *roots, options=("--report", str(report)), expect_error=not upfront
    )

    reads = [
        json.loads(line.partition(" ")[2])
        for line in result.stdout.splitlines()
        if line.startswith("DEPENDENCY_READ ")
    ]
    assert preparation_count(result.stdout, rejected) == 1
    assert any(source.as_uri() in requirement for requirement in reads)
    assert "missing==1" in reads
    assert "Traceback" not in result.stderr
    if not upfront:
        assert result.returncode == 1
        script.assert_not_installed("app", "dep", "left", "right")
        return

    expected.update(app=retained)
    expected[name] = source
    selected = {
        item["metadata"]["name"]: item
        for item in json.loads(report.read_text())["install"]
    }
    assert set(selected) == set(expected)
    for package, artifact in expected.items():
        download = selected[package]["download_info"]
        assert download["url"] == artifact.as_uri()
        assert (
            download["archive_info"]["hashes"]["sha256"]
            == hashlib.sha256(artifact.read_bytes()).hexdigest()
        )
    assert selected[name]["is_direct"] is True
    if shape != "extras":
        assert selected["right"]["is_direct"] is (shape == "url-cycle")
    expected_versions = {"app": "1", name: version}
    if shape != "extras":
        expected_versions["right"] = "2"
    assert {
        package: item["metadata"]["version"] for package, item in selected.items()
    } == expected_versions
    script.assert_installed(**expected_versions)
    script.pip("check")


@pytest.mark.parametrize(
    "reconsider", [False, True], ids=["lower-parent", "higher-parent"]
)
def test_named_parent_uses_exact_url_child_without_index_query(
    script: PipTestEnvironment, mock_server: MockServer, reconsider: bool
) -> None:
    child = create_basic_wheel_for_package(script, "uri-child", "1")
    child_requirement = f"uri-child @ {child.as_uri()}"
    expected = {"uri-child": ("1", child)}
    auxiliary = None
    if reconsider:
        auxiliary = create_basic_wheel_for_package(script, "uri-aux", "2")
        metadata = (
            "Metadata-Version: 2.1\nName: uri-child\nVersion: 1\n"
            f"Requires-Dist: uri-aux @ {auxiliary.as_uri()}\n"
        )
        make_wheel("uri_child", "1", metadata=metadata).save_to(child)
        metadata = (
            "Metadata-Version: 2.1\nName: uri-parent\nVersion: 2\n"
            "Requires-Dist: uri-aux>=2\n"
            f"Requires-Dist: {child_requirement}\n"
        )
        latest = script.scratch_path / "uri_parent-2-py3-none-any.whl"
        make_wheel("uri_parent", "2", metadata=metadata).save_to(latest)
        expected["uri-aux"] = ("2", auxiliary)
    else:
        latest = create_basic_wheel_for_package(
            script, "uri-parent", "2", depends=["absent-child==1"]
        )

    # Raw headers keep the declared child URL intact.
    metadata = (
        "Metadata-Version: 2.1\nName: uri-parent\nVersion: 1\n"
        f"Requires-Dist: {child_requirement}\n"
    )
    older = script.scratch_path / "uri_parent-1-py3-none-any.whl"
    make_wheel("uri_parent", "1", metadata=metadata).save_to(older)
    expected["uri-parent"] = ("2", latest) if reconsider else ("1", older)

    def index(
        environ: "WSGIEnvironment", start_response: "StartResponse"
    ) -> list[bytes]:
        body = b""
        if environ["PATH_INFO"] == "/simple/uri-parent/":
            body = "\n".join(
                f'<a href="{wheel.as_uri()}">{wheel.name}</a>'
                for wheel in (latest, older)
            ).encode()
        start_response("200 OK", [("Content-Type", "text/html")])
        return [body]

    mock_server.set_responses(repeat(index))
    mock_server.start()
    probe = script.scratch_path / "preparation_probe.py"
    probe.write_text(PREPARATION_PROBE)
    report = script.scratch_path / "exact-child-report.json"
    try:
        result = script.run(
            "python",
            str(probe),
            "install",
            "--no-cache-dir",
            "--index-url",
            f"http://{mock_server.host}:{mock_server.port}/simple",
            "--report",
            str(report),
            "uri-parent",
        )
    finally:
        mock_server.stop()

    requests = mock_server.get_requests()
    assert any(request["PATH_INFO"] == "/simple/uri-parent/" for request in requests)
    assert not any(request["PATH_INFO"] == "/simple/uri-child/" for request in requests)
    lines = result.stdout.splitlines()
    restarts = [
        position
        for position, line in enumerate(lines)
        if line.startswith("Nab source-domain restart:")
    ]
    assert len(restarts) == (2 if reconsider else 1)
    assert preparation_count(result.stdout, older) == 1
    if reconsider:
        older_read = lines.index("PREPARE " + older.name)
        assert restarts[0] < older_read < restarts[1]
        assert auxiliary is not None
        assert any(
            auxiliary.as_uri() in line
            for line in lines
            if line.startswith("DEPENDENCY_READ ")
        )

    selected = {
        canonicalize_name(item["metadata"]["name"]): item
        for item in json.loads(report.read_text())["install"]
    }
    assert set(selected) == set(expected)
    for name, (version, artifact) in expected.items():
        selected_item = selected[canonicalize_name(name)]
        assert selected_item["metadata"]["version"] == version
        download = selected_item["download_info"]
        assert download["url"] == artifact.as_uri()
        assert (
            download["archive_info"]["hashes"]["sha256"]
            == hashlib.sha256(artifact.read_bytes()).hexdigest()
        )
    assert selected[canonicalize_name("uri-parent")]["is_direct"] is False
    assert selected[canonicalize_name("uri-child")]["is_direct"] is True
    script.assert_installed(
        **{name: version for name, (version, _artifact) in expected.items()}
    )
    script.pip("check")
