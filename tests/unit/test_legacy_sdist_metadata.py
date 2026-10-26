from pathlib import Path

import pytest

from pip._internal.distributions.sdist import (
    SDIST_RESOLUTION_FIELDS,
    parse_sdist_identity,
    read_sdist_metadata,
)


@pytest.mark.parametrize(
    "extension",
    [
        ".zip",
        ".tar.gz",
        ".tgz",
        ".tar",
        ".tar.bz2",
        ".tbz",
        ".tar.xz",
        ".txz",
        ".tlz",
        ".tar.lzma",
        ".tar.lz",
        ".ZIP",
    ],
)
def test_legacy_archive_metadata_supplies_static_fields(
    tmp_path: Path, extension: str
) -> None:
    filename = f"demo-package-1.0+local{extension}"
    (tmp_path / "PKG-INFO").write_text(
        "Metadata-Version: 2.2\nName: demo-package\nVersion: 1.0+local\n"
        "Requires-Dist: dependency==1.0\n"
    )

    dist = read_sdist_metadata(str(tmp_path), filename, SDIST_RESOLUTION_FIELDS)

    assert dist is not None
    assert dist.canonical_name == "demo-package"
    assert str(dist.version) == "1.0+local"
    assert [str(req) for req in dist.iter_dependencies()] == ["dependency==1.0"]
    assert parse_sdist_identity(filename) is None


@pytest.mark.parametrize(
    "filename",
    [
        "main.zip",
        "source.zip",
        "demo-package-main.zip",
        "demo_package-1.0.whl",
        "demo_package-1.0.egg",
        "demo_package-1.0.data",
        "other-1.0.zip",
        "demo-package-2.0.zip",
    ],
)
def test_legacy_archive_requires_matching_filename_identity(
    tmp_path: Path, filename: str
) -> None:
    (tmp_path / "PKG-INFO").write_text(
        "Metadata-Version: 2.2\nName: demo-package\nVersion: 1.0\n"
    )

    assert read_sdist_metadata(str(tmp_path), filename, SDIST_RESOLUTION_FIELDS) is None


def test_legacy_version_with_extra_dash_retains_backend_fallback(
    tmp_path: Path,
) -> None:
    (tmp_path / "PKG-INFO").write_text(
        "Metadata-Version: 2.2\nName: demo-package\nVersion: 1.0.post1\n"
    )

    assert (
        read_sdist_metadata(
            str(tmp_path), "demo-package-1.0-1.zip", SDIST_RESOLUTION_FIELDS
        )
        is None
    )


@pytest.mark.parametrize("filename", ["demo-1.0.zip", "demo-package-1.0.tar.gz"])
@pytest.mark.parametrize(
    "metadata", [None, "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n"]
)
def test_legacy_filename_alone_does_not_supply_identity(
    tmp_path: Path, filename: str, metadata: str | None
) -> None:
    if metadata is not None:
        (tmp_path / "PKG-INFO").write_text(metadata)

    assert read_sdist_metadata(str(tmp_path), filename, frozenset()) is None
