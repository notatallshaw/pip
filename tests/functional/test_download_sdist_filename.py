from pathlib import Path

import pytest

from pip._internal.utils.compat import tomllib

from tests.lib import PipTestEnvironment, create_basic_sdist_for_package


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        "Metadata-Version: 2.1\nName: known-name\nVersion: 1.0\n\n",
        "Metadata-Version: 2.2\nName: different-name\nVersion: 9.0\n\n",
        (
            "Metadata-Version: 2.2\nName: known-name\nVersion: 1.0\n"
            "Requires-Dist: dependency>=broken\n"
        ),
    ],
    ids=["missing", "legacy", "mismatched", "unused-invalid-dependency"],
)
@pytest.mark.parametrize("command", ["download", "lock", "install"])
def test_standardized_filename_when_only_identity_is_needed(
    script: PipTestEnvironment, metadata: str | None, command: str
) -> None:
    files = {
        "pyproject.toml": (
            '[build-system]\nrequires = []\nbuild-backend = "unavailable_backend"\n'
        ),
    }
    if metadata is not None:
        files["PKG-INFO"] = metadata
    archive = create_basic_sdist_for_package(
        script, "known_name", "1.0", extra_files=files
    )

    args = [command, "--no-index", "--no-deps", "--ignore-requires-python"]
    if command == "download":
        args.extend(["--dest", "downloaded"])
    elif command == "install":
        args.extend(["--dry-run", "--ignore-installed"])
    result = script.pip(*args, archive, allow_stderr_warning=command == "lock")

    if command == "download":
        result.did_create(Path("scratch/downloaded/known_name-1.0.tar.gz"))
        downloaded = script.scratch_path / "downloaded" / archive.name
        assert downloaded.read_bytes() == archive.read_bytes()
        assert "Successfully downloaded known-name" in result.stdout
    elif command == "lock":
        result.did_create(Path("scratch/pylock.toml"))
        lock = tomllib.loads(script.scratch_path.joinpath("pylock.toml").read_text())
        assert [package["name"] for package in lock["packages"]] == ["known-name"]
    else:
        assert "Would install known-name-1.0" in result.stdout


@pytest.mark.parametrize(
    "flags, filename",
    [
        ([], "known_name-1.0.tar.gz"),
        (["--no-deps"], "known_name-1.0.tar.gz"),
        (["--ignore-requires-python"], "known_name-1.0.tar.gz"),
        (
            ["--no-deps", "--ignore-requires-python"],
            "known-name-1.0.tar.gz",
        ),
    ],
)
def test_filename_metadata_requires_both_flags_and_standard_filename(
    script: PipTestEnvironment, flags: list[str], filename: str
) -> None:
    archive = create_basic_sdist_for_package(
        script, "known_name", "1.0", fails_build=True
    )
    renamed = archive.with_name(filename)
    archive.rename(renamed)

    result = script.pip(
        "download",
        "--no-index",
        "--no-build-isolation",
        *flags,
        "--dest",
        "downloaded",
        renamed,
        expect_error=True,
    )

    assert "Simulated build failure" in result.stderr


def test_filename_metadata_keeps_archive_hash_checks(
    script: PipTestEnvironment,
) -> None:
    archive = create_basic_sdist_for_package(script, "known_name", "1.0")
    result = script.pip(
        "download",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--dest",
        "downloaded",
        f"{archive.as_uri()}#sha256={'0' * 64}",
        expect_error=True,
    )

    assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr
    result.did_not_create(Path("scratch/downloaded/known_name-1.0.tar.gz"))


def test_filename_metadata_keeps_archive_format_checks(
    script: PipTestEnvironment,
) -> None:
    archive = script.scratch_path / "known_name-1.0.tar.gz"
    archive.write_bytes(b"not a source archive")
    result = script.pip(
        "download",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--dest",
        "downloaded",
        archive,
        expect_error=True,
    )

    result.did_not_create(Path("scratch/downloaded/known_name-1.0.tar.gz"))
