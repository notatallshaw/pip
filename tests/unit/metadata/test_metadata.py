import csv
import importlib.metadata
import io
import logging
import os
from pathlib import Path
from typing import cast
from unittest import mock

import pytest

from pip._vendor.packaging.utils import NormalizedName, canonicalize_name

from pip._internal.metadata import (
    BaseDistribution,
    get_directory_distribution,
    get_environment,
    get_wheel_distribution,
)
from pip._internal.metadata.base import FilesystemWheel
from pip._internal.metadata.importlib import Distribution as ImportlibDistribution
from pip._internal.metadata.importlib import Environment as ImportlibEnvironment
from pip._internal.metadata.pkg_resources import (
    Distribution as PkgResourcesDistribution,
)
from pip._internal.models.direct_url import DIRECT_URL_METADATA_NAME

from tests.lib.wheel import RECORD_FILENAMES, make_wheel


def _record_text(terminator: str) -> str:
    """Build RECORD rows with quoted line breaks on every supported Python."""
    record = io.StringIO(newline="")
    # Older CSV writers only quote line breaks present in the row terminator.
    writer = csv.writer(
        record,
        lineterminator=terminator,
        quoting=csv.QUOTE_MINIMAL if terminator == "\r\n" else csv.QUOTE_ALL,
    )
    writer.writerows((filename, "", "") for filename in RECORD_FILENAMES)
    return record.getvalue()


@pytest.mark.parametrize(
    "backend",
    [ImportlibDistribution, PkgResourcesDistribution],
    ids=["importlib", "pkg_resources"],
)
@pytest.mark.parametrize("storage", ["directory", "wheel"])
@pytest.mark.parametrize("terminator", ["\n", "\r\n", "\r"])
def test_declared_entries_preserve_record_filenames(
    tmp_path: Path,
    backend: type[BaseDistribution],
    storage: str,
    terminator: str,
) -> None:
    record = _record_text(terminator)
    if storage == "directory":
        info = tmp_path / "simple-1.0.dist-info"
        info.mkdir()
        (info / "RECORD").write_bytes(record.encode("utf-8"))
        dist = backend.from_directory(str(info))
    else:
        wheel_path = make_wheel("simple", "1.0", record=record).save_to_dir(tmp_path)
        dist = backend.from_wheel(
            FilesystemWheel(wheel_path), canonicalize_name("simple")
        )

    entries = dist.iter_declared_entries()
    assert entries is not None
    assert list(entries) == list(RECORD_FILENAMES)


@pytest.mark.parametrize("state", ["missing", "empty", "directory", "legacy-file"])
def test_importlib_unavailable_record(tmp_path: Path, state: str) -> None:
    info = tmp_path / "simple-1.0.dist-info"
    if state == "legacy-file":
        info = tmp_path / "simple.egg-info"
        info.write_bytes(b"Name: simple\nVersion: 1.0\n")
    else:
        info.mkdir()
        if state == "empty":
            (info / "RECORD").write_bytes(b"")
        elif state == "directory":
            (info / "RECORD").mkdir()

    dist = ImportlibDistribution.from_directory(str(info))
    entries = dist.iter_declared_entries()
    if state == "empty":
        assert entries is not None
        assert list(entries) == []
    else:
        assert entries is None


def test_importlib_non_record_newline_handling(tmp_path: Path) -> None:
    info = tmp_path / "simple-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_bytes(b"Name: simple\r\nVersion: 1.0\r")
    dist = ImportlibDistribution.from_directory(str(info))

    assert dist.read_text("METADATA") == "Name: simple\nVersion: 1.0\n"


def test_importlib_record_without_metadata_location(tmp_path: Path) -> None:
    info = tmp_path / "simple-1.0.dist-info"
    info.mkdir()
    (info / "RECORD").write_bytes(b"plain.dat,,\n")
    underlying = importlib.metadata.Distribution.at(info)
    dist = ImportlibDistribution(underlying, None, None)

    assert dist.read_text("RECORD") == "plain.dat,,\n"


@pytest.mark.parametrize("terminator", ["\n", "\r\n", "\r"])
def test_declared_entries_preserve_zipped_record_filenames(
    tmp_path: Path, terminator: str
) -> None:
    location = str(tmp_path / "simple.zip")
    make_wheel("simple", "1.0", record=_record_text(terminator)).save_to(location)
    dist = ImportlibEnvironment.from_paths([location]).get_distribution("simple")

    assert dist is not None
    entries = dist.iter_declared_entries()
    assert entries is not None
    assert list(entries) == list(RECORD_FILENAMES)


@mock.patch.object(BaseDistribution, "read_text", side_effect=FileNotFoundError)
def test_dist_get_direct_url_no_metadata(mock_read_text: mock.Mock) -> None:
    class FakeDistribution(BaseDistribution):
        pass

    dist = FakeDistribution()  # type: ignore
    assert dist.direct_url is None
    mock_read_text.assert_called_once_with(DIRECT_URL_METADATA_NAME)


@mock.patch.object(BaseDistribution, "read_text", return_value="{}")
def test_dist_get_direct_url_invalid_json(
    mock_read_text: mock.Mock, caplog: pytest.LogCaptureFixture
) -> None:
    class FakeDistribution(BaseDistribution):
        canonical_name = cast(NormalizedName, "whatever")  # Needed for error logging.

    dist = FakeDistribution()  # type: ignore
    with caplog.at_level(logging.WARNING):
        assert dist.direct_url is None

    mock_read_text.assert_called_once_with(DIRECT_URL_METADATA_NAME)
    assert (
        caplog.records[-1]
        .getMessage()
        .startswith(
            "Error parsing direct_url.json for whatever:",
        )
    )


def test_metadata_reads_egg_info_requires_txt(tmp_path: Path) -> None:
    """Check Requires-Dist is obtained from requires.txt if absent in PKG-INFO."""
    egg_info_path = tmp_path / "whatever.egg-info"
    egg_info_path.mkdir()
    dist = get_directory_distribution(str(egg_info_path))
    assert dist.installed_with_setuptools_egg_info
    pkg_info_path = egg_info_path / "PKG-INFO"
    pkg_info_path.write_text("Name: whatever\n")
    egg_info_path.joinpath("requires.txt").write_text("pkga\npkgb\n")
    assert dist.metadata.get_all("Requires-Dist") == ["pkga", "pkgb"]


def test_metadata_pkg_info_requires_priority(tmp_path: Path) -> None:
    """Check Requires-Dist in PKG-INFO has priority over requires.txt."""
    egg_info_path = tmp_path / "whatever.egg-info"
    egg_info_path.mkdir()
    dist = get_directory_distribution(str(egg_info_path))
    assert dist.installed_with_setuptools_egg_info
    pkg_info_path = egg_info_path / "PKG-INFO"
    pkg_info_path.write_text(
        "Name: whatever\nRequires-Dist: pkgc\nRequires-Dist: pkgd\n"
    )
    egg_info_path.joinpath("requires.txt").write_text("pkga\npkgb\n")
    assert dist.metadata.get_all("Requires-Dist") == ["pkgc", "pkgd"]


@mock.patch.object(
    BaseDistribution,
    "read_text",
    return_value='{"url": "https://e.c/p.tgz", "archive_info": {}}',
)
def test_dist_get_direct_url_valid_metadata(mock_read_text: mock.Mock) -> None:
    class FakeDistribution(BaseDistribution):
        pass

    dist = FakeDistribution()  # type: ignore
    direct_url = dist.direct_url
    assert direct_url is not None
    mock_read_text.assert_called_once_with(DIRECT_URL_METADATA_NAME)
    assert direct_url.url == "https://e.c/p.tgz"
    assert direct_url.archive_info


def test_metadata_dict(tmp_path: Path) -> None:
    """Basic test of BaseDistribution metadata_dict.

    More tests are available in the original pkg_metadata project where this
    function comes from, and which we may vendor in the future.
    """
    wheel_path = make_wheel(name="pkga", version="1.0.1").save_to_dir(tmp_path)
    wheel = FilesystemWheel(wheel_path)
    dist = get_wheel_distribution(wheel, canonicalize_name("pkga"))
    metadata_dict = dist.metadata_dict
    assert metadata_dict["name"] == "pkga"
    assert metadata_dict["version"] == "1.0.1"


def test_no_dist_found_in_wheel(tmp_path: Path) -> None:
    location = os.fspath(tmp_path.joinpath("pkg-1-py3-none-any.whl"))
    make_wheel(name="pkg", version="1").save_to(location)
    assert get_environment([location]).get_distribution("pkg") is None


def test_dist_found_in_directory_named_whl(tmp_path: Path) -> None:
    dir_path = tmp_path.joinpath("pkg-1-py3-none-any.whl")
    info_path = dir_path.joinpath("pkg-1.dist-info")
    info_path.mkdir(parents=True)
    info_path.joinpath("METADATA").write_text("Name: pkg")
    location = os.fspath(dir_path)
    dist = get_environment([location]).get_distribution("pkg")
    assert dist is not None
    assert dist.location is not None
    assert Path(dist.location) == Path(location)


def test_dist_found_in_zip(tmp_path: Path) -> None:
    location = os.fspath(tmp_path.joinpath("pkg.zip"))
    make_wheel(name="pkg", version="1").save_to(location)
    dist = get_environment([location]).get_distribution("pkg")
    assert dist is not None
    assert dist.location is not None
    assert Path(dist.location) == Path(location)


@pytest.mark.parametrize("trailing_slash", [False, True])
def test_trailing_slash_directory_metadata(
    tmp_path: Path, trailing_slash: bool
) -> None:
    # Tests issue fixed by https://github.com/pypa/pip/pull/2530
    egg_info = tmp_path / "foo.egg-info"
    egg_info.mkdir()
    (egg_info / "PKG-INFO").write_text("Metadata-Version: 1.0\nName: foo\n")
    path = str(egg_info) + (os.sep if trailing_slash else "")
    dist = get_directory_distribution(path)
    assert dist.raw_name == dist.canonical_name == "foo"
    assert dist.location == str(tmp_path)
