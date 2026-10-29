from pathlib import Path

import pytest

from pip._internal.distributions.sdist import read_sdist_metadata


@pytest.mark.parametrize(
    "filename, name, version",
    [
        ("demo-1.0.tar.gz", "demo", "1.0"),
        ("demo_package-1.2rc1.tar.gz", "demo-package", "1.2rc1"),
        ("demo_package-1.0+local.tar.gz", "demo-package", "1.0+local"),
    ],
)
def test_sdist_filename_metadata(
    tmp_path: Path, filename: str, name: str, version: str
) -> None:
    dist = read_sdist_metadata(str(tmp_path), filename, frozenset())

    assert dist is not None
    assert dist.canonical_name == name
    assert str(dist.version) == version
    assert list(dist.iter_dependencies()) == []
    assert not str(dist.requires_python)
    assert set(dist.metadata.get_all("Dynamic", [])) == {
        "Requires-Dist",
        "Provides-Extra",
        "Requires-Python",
    }


@pytest.mark.parametrize(
    "filename",
    [
        "demo-1.0.zip",
        "demo-1.0.tar.bz2",
        "demo.tar.gz",
        "demo-package-1.0.tar.gz",
        "-1.0.tar.gz",
        "demo-.tar.gz",
        "demo-invalid.tar.gz",
        "Demo.Package-1.0.tar.gz",
        "demo.package-1.0.tar.gz",
        "demo-01.0.tar.gz",
        "demo!-1.0.tar.gz",
        "demo\n-1.0.tar.gz",
    ],
)
def test_sdist_filename_metadata_rejects_nonstandard_name(
    tmp_path: Path, filename: str
) -> None:
    assert read_sdist_metadata(str(tmp_path), filename, frozenset()) is None


@pytest.mark.parametrize(
    "required", ["requires-dist", "provides-extra", "requires-python", "*"]
)
def test_sdist_filename_does_not_supply_other_metadata(
    tmp_path: Path, required: str
) -> None:
    assert (
        read_sdist_metadata(str(tmp_path), "demo-1.0.tar.gz", frozenset({required}))
        is None
    )
