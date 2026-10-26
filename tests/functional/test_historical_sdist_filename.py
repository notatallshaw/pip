from __future__ import annotations

from pathlib import Path

import pytest

from tests.lib import PipTestEnvironment, create_basic_sdist_for_package


@pytest.mark.parametrize("metadata_version", [None, "2.1", "2.2"])
def test_historical_filename_requires_modern_identity_metadata(
    script: PipTestEnvironment, metadata_version: str | None
) -> None:
    files = {
        "pyproject.toml": (
            '[build-system]\nrequires = []\nbuild-backend = "unavailable_backend"\n'
        )
    }
    if metadata_version is not None:
        files["PKG-INFO"] = (
            f"Metadata-Version: {metadata_version}\n"
            "Name: demo.package\nVersion: 1.0\n\n"
        )
    archive = create_basic_sdist_for_package(
        script, "demo_package", "1.0", extra_files=files
    ).rename(script.scratch_path / "Demo.Package-1.0.tar.gz")

    result = script.pip(
        "download",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--dest",
        "downloaded",
        archive,
        expect_error=metadata_version != "2.2",
    )

    if metadata_version == "2.2":
        result.did_create(Path("scratch/downloaded/Demo.Package-1.0.tar.gz"))
    else:
        assert "backend-unavailable" in result.stderr
