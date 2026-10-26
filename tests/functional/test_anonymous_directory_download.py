import os
from pathlib import Path

import pytest

from tests.lib import PipTestEnvironment


@pytest.fixture
def anonymous_directory(script: PipTestEnvironment) -> Path:
    project = script.scratch_path / "project"
    project.mkdir()
    project.joinpath("pyproject.toml").write_text(
        '[build-system]\nrequires = ["unavailable-build-backend"]\n'
        'build-backend = "backend"\nbackend-path = ["."]\n'
    )
    project.joinpath("backend.py").write_text(
        'raise RuntimeError("Unexpected backend execution")\n'
    )
    return project


@pytest.mark.parametrize("resolver", ["resolvelib", "legacy"])
@pytest.mark.parametrize("source", ["path", "uri"])
def test_anonymous_directory_download_creates_no_artifact(
    script: PipTestEnvironment, anonymous_directory: Path, resolver: str, source: str
) -> None:
    script.environ["PIP_USE_DEPRECATED"] = (
        "legacy-resolver" if resolver == "legacy" else ""
    )
    contents = {path.name: path.read_bytes() for path in anonymous_directory.iterdir()}
    requirement = (
        str(anonymous_directory) if source == "path" else anonymous_directory.as_uri()
    )
    result = script.pip(
        "download",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--dest",
        "downloaded",
        requirement,
        *(["--no-build-isolation"] if resolver == "legacy" else []),
        expect_error=resolver == "legacy",
    )
    if resolver == "legacy":
        assert "Unexpected backend execution" in result.stderr
        assert list(script.scratch_path.joinpath("downloaded").iterdir()) == []
        return
    assert (
        f"Source directory already available: {os.path.join('.', 'project')}"
        in result.stdout
    )
    assert list(script.scratch_path.joinpath("downloaded").iterdir()) == []
    assert {
        path.name: path.read_bytes() for path in anonymous_directory.iterdir()
    } == contents


@pytest.mark.parametrize("guard", ["deps", "python", "named", "check-build"])
def test_other_directory_downloads_keep_existing_checks(
    script: PipTestEnvironment, anonymous_directory: Path, guard: str
) -> None:
    flags = ["--no-deps", "--ignore-requires-python", "--no-build-isolation"]
    requirement = anonymous_directory.as_uri()
    if guard == "deps":
        flags.remove("--no-deps")
    elif guard == "python":
        flags.remove("--ignore-requires-python")
    elif guard == "named":
        requirement = f"project @ {requirement}"
    else:
        flags.append("--check-build-dependencies")
    result = script.pip(
        "download", "--no-index", *flags, requirement, expect_error=True
    )
    if guard == "check-build":
        assert "are missing" in result.stderr
        assert "Unexpected backend execution" not in result.stderr
    else:
        assert "Unexpected backend execution" in result.stderr


def test_anonymous_directory_download_still_rejects_required_hashes(
    script: PipTestEnvironment, anonymous_directory: Path
) -> None:
    result = script.pip(
        "download",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--require-hashes",
        anonymous_directory,
        expect_error=True,
    )
    assert "Can't verify hashes for these file:// requirements" in result.stderr
    assert "Unexpected backend execution" not in result.stderr


def test_anonymous_directory_download_validates_declarations(
    script: PipTestEnvironment, anonymous_directory: Path
) -> None:
    anonymous_directory.joinpath("pyproject.toml").write_text(
        '[build-system]\nrequires = "invalid"\n'
    )
    result = script.pip(
        "download",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        anonymous_directory,
        expect_error=True,
    )
    assert "build-system.requires" in result.stderr
    assert "Traceback" not in result.stderr
    assert "Unexpected backend execution" not in result.stderr
