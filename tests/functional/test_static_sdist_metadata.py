import json
import shutil
from pathlib import Path

import pytest

from tests.lib import PipTestEnvironment, create_basic_sdist_for_package, git_commit
from tests.lib.wheel import make_wheel


@pytest.fixture
def static_sdist(script: PipTestEnvironment) -> Path:
    return create_basic_sdist_for_package(
        script,
        "static_demo",
        "1.0",
        extra_files={
            "PKG-INFO": (
                "Metadata-Version: 2.2\nName: static-demo\nVersion: 1.0\n"
                "Requires-Python: >=3.9\nRequires-Dist: dependency==1.0\n"
                "Provides-Extra: speed\n"
                'Requires-Dist: accelerator==1.0; extra == "speed"\n'
                "Summary: Static project\n\nProject description.\n"
            ),
            "pyproject.toml": (
                '[build-system]\nrequires = ["unavailable-build-backend"]\n'
                'build-backend = "backend"\nbackend-path = ["."]\n'
            ),
            "backend.py": 'raise RuntimeError("backend must not execute")\n',
        },
    )


@pytest.mark.parametrize("command", ["download", "lock", "install"])
def test_static_sdist_without_backend(
    script: PipTestEnvironment, static_sdist: Path, command: str
) -> None:
    wheelhouse = script.scratch_path / "wheelhouse"
    wheelhouse.mkdir()
    for name in ("dependency", "accelerator"):
        make_wheel(name, "1.0").save_to_dir(wheelhouse)
    args: list[str | Path] = [command, "--no-index", "--find-links", wheelhouse]
    if command == "install":
        args.extend(["--dry-run", "--ignore-installed", "--report", "report.json"])
    if command == "lock":
        args.extend(["--output", "pylock.toml"])
    result = script.pip(
        *args,
        f"static-demo[speed] @ {static_sdist.as_uri()}",
        allow_stderr_warning=(command == "lock"),
    )
    if command == "download":
        result.did_create(Path("scratch") / "dependency-1.0-py2.py3-none-any.whl")
        result.did_create(Path("scratch") / "accelerator-1.0-py2.py3-none-any.whl")
    elif command == "install":
        report = json.loads(script.scratch_path.joinpath("report.json").read_text())
        package = next(item for item in report["install"] if item["requested"])
        assert package["metadata"]["summary"] == "Static project"
        assert package["metadata"]["description"] == "Project description.\n"
        assert {item["metadata"]["name"] for item in report["install"]} == {
            "static-demo",
            "dependency",
            "accelerator",
        }
    else:
        result.did_create(Path("scratch") / "pylock.toml")


@pytest.mark.parametrize(
    "metadata_update",
    ["Metadata-Version: 2.1", "Dynamic: Requires-Dist"],
)
def test_dynamic_or_legacy_sdist_uses_backend(
    script: PipTestEnvironment, metadata_update: str
) -> None:
    metadata = "Metadata-Version: 2.2\nName: demo\nVersion: 1.0\n"
    if metadata_update.startswith("Metadata-Version"):
        metadata = metadata.replace("Metadata-Version: 2.2", metadata_update)
    else:
        metadata += metadata_update + "\n"
    archive = create_basic_sdist_for_package(
        script,
        "demo",
        "1.0",
        extra_files={"PKG-INFO": metadata},
        fails_build=True,
    )
    result = script.pip(
        "download", "--no-index", "--no-build-isolation", archive, expect_error=True
    )
    assert "Simulated build failure" in result.stderr


@pytest.mark.parametrize("report", [False, True])
def test_dynamic_report_metadata_needs_backend(
    script: PipTestEnvironment, report: bool
) -> None:
    archive = create_basic_sdist_for_package(
        script,
        "demo",
        "1.0",
        extra_files={
            "PKG-INFO": (
                "Metadata-Version: 2.2\nName: demo\nVersion: 1.0\n"
                "Dynamic: Summary\nSummary: provisional\n"
            ),
            "pyproject.toml": (
                '[build-system]\nrequires = []\nbuild-backend = "backend"\n'
                'backend-path = ["."]\n'
            ),
            "backend.py": 'raise RuntimeError("backend required for report")\n',
        },
    )
    args = [
        "install",
        "--dry-run",
        "--ignore-installed",
        "--no-index",
        "--no-build-isolation",
    ]
    if report:
        args += ["--report", "report.json"]
    result = script.pip(*args, archive, expect_error=report)
    if report:
        assert "backend required for report" in result.stderr


def test_static_sdist_checks_archive_hash(
    script: PipTestEnvironment, static_sdist: Path
) -> None:
    result = script.pip(
        "download",
        "--no-index",
        "--no-deps",
        "--dest",
        "download",
        f"{static_sdist.as_uri()}#sha256={'0' * 64}",
        expect_error=True,
    )
    assert "DO NOT MATCH THE HASHES" in result.stderr


@pytest.mark.parametrize("vcs", [False, True])
def test_source_tree_ignores_stale_sdist_metadata(
    script: PipTestEnvironment, static_sdist: Path, vcs: bool
) -> None:
    # An archive-looking directory is still a source tree.
    source = script.scratch_path / "source" / static_sdist.name
    shutil.unpack_archive(static_sdist, source)
    if vcs:
        script.run("git", "init", "--quiet", cwd=source)
        script.run("git", "add", ".", cwd=source)
        git_commit(source)
        requirement = f"git+{source.as_uri()}"
    else:
        requirement = str(source)
    result = script.pip(
        "download",
        "--no-index",
        "--no-build-isolation",
        requirement,
        expect_error=True,
    )
    assert "backend must not execute" in result.stderr


def test_subdirectory_ignores_archive_root_metadata(script: PipTestEnvironment) -> None:
    archive = create_basic_sdist_for_package(
        script,
        "demo",
        "1.0",
        extra_files={
            "PKG-INFO": "Metadata-Version: 2.2\nName: demo\nVersion: 1.0\n",
            "child/setup.py": 'raise RuntimeError("child backend required")\n',
        },
    )
    result = script.pip(
        "download",
        "--no-index",
        "--no-build-isolation",
        f"{archive.as_uri()}#subdirectory=child",
        expect_error=True,
    )
    assert "child backend required" in result.stderr


def test_static_sdist_validates_build_system(script: PipTestEnvironment) -> None:
    archive = create_basic_sdist_for_package(
        script,
        "demo",
        "1.0",
        extra_files={
            "PKG-INFO": "Metadata-Version: 2.2\nName: demo\nVersion: 1.0\n",
            "pyproject.toml": '[build-system]\nrequires = "not a list"\n',
        },
    )
    result = script.pip("download", "--no-index", archive, expect_error=True)
    assert "invalid `build-system.requires` key" in result.stderr


def test_static_sdist_preserves_direct_dependency_url(
    script: PipTestEnvironment,
) -> None:
    files = {
        "pyproject.toml": (
            '[build-system]\nrequires = []\nbuild-backend = "backend"\n'
            'backend-path = ["."]\n'
        ),
        "backend.py": 'raise AssertionError("backend must not execute")\n',
    }
    dependency = create_basic_sdist_for_package(
        script,
        "dependency",
        "1.0",
        extra_files={
            **files,
            "PKG-INFO": "Metadata-Version: 2.2\nName: dependency\nVersion: 1.0\n",
        },
    )
    archive = create_basic_sdist_for_package(
        script,
        "demo",
        "1.0",
        extra_files={
            **files,
            "PKG-INFO": (
                "Metadata-Version: 2.2\nName: demo\nVersion: 1.0\n"
                f"Requires-Dist: dependency @ {dependency.as_uri()}\n"
                "Dynamic: Summary\nSummary: provisional\n"
            ),
        },
    )
    result = script.pip("download", "--no-index", "--dest", "download", archive)
    result.did_create(Path("scratch") / "download" / dependency.name)
