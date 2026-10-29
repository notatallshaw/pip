from pathlib import Path

import pytest

from pip._internal.distributions.sdist import (
    SDIST_RESOLUTION_FIELDS,
    parse_sdist_identity,
    read_sdist_metadata,
)


@pytest.mark.parametrize("metadata_version", ["2.2", "2.4", "2.6"])
def test_static_sdist_metadata(tmp_path: Path, metadata_version: str) -> None:
    metadata = (
        f"Metadata-Version: {metadata_version}\n"
        "Name: static-demo\nVersion: 1.0\nRequires-Python: >=3.10\n"
        "Requires-Dist: dependency>=1\nProvides-Extra: speed\n"
        'Requires-Dist: accelerator; extra == "speed"\n\n'
    )
    (tmp_path / "PKG-INFO").write_text(metadata)
    dist = read_sdist_metadata(
        str(tmp_path), "static_demo-1.0.tar.gz", SDIST_RESOLUTION_FIELDS
    )
    assert dist is not None
    assert dist.canonical_name == "static-demo"
    assert str(dist.version) == "1.0"
    assert str(dist.requires_python) == ">=3.10"
    assert {str(r) for r in dist.iter_dependencies()} == {"dependency>=1"}
    assert {str(r) for r in dist.iter_dependencies(["speed"])} == {
        "dependency>=1",
        'accelerator; extra == "speed"',
    }


@pytest.mark.parametrize(
    "metadata",
    [
        "",
        "Name: demo\nVersion: 1.0\n",
        "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n",
        "Metadata-Version: 3.0\nName: demo\nVersion: 1.0\n",
        "Metadata-Version: 2.2\nVersion: 1.0\n",
        "Metadata-Version: 2.2\nName: demo\n",
        "Metadata-Version: 2.2\nName: demo\nVersion: invalid\n",
        "Metadata-Version: 2.2\nName: demo\nName: other\nVersion: 1.0\n",
        "Metadata-Version: 2.2\nName: demo\nVersion: 1.0\nDynamic: Name\n",
        "Metadata-Version: 2.2\nName: demo\nVersion: 1.0\nDynamic: Requires-Dist\n",
        "Metadata-Version: 2.2\nName: demo\nVersion: 1.0\nDynamic: Requires-Python\n",
        "Metadata-Version: 2.2\nName: demo\nVersion: 1.0\nDynamic: Provides-Extra\n",
        "Metadata-Version: 2.2\nName: demo\nVersion: 1.0\nDynamic: Not A Field\n",
        "Metadata-Version: 2.2\nName: demo\nVersion: 1.0\nbroken header\n",
    ],
)
def test_unusable_sdist_metadata(tmp_path: Path, metadata: str) -> None:
    (tmp_path / "PKG-INFO").write_text(metadata)
    assert (
        read_sdist_metadata(str(tmp_path), "demo-1.0.tar.gz", SDIST_RESOLUTION_FIELDS)
        is None
    )


def test_missing_sdist_metadata(tmp_path: Path) -> None:
    assert (
        read_sdist_metadata(str(tmp_path), "demo-1.0.tar.gz", SDIST_RESOLUTION_FIELDS)
        is None
    )


@pytest.mark.parametrize(
    "filename",
    [
        "demo-1.0.zip",
        "demo-1.0.tgz",
        "demo-main.tar.gz",
        "other-1.0.tar.gz",
        "demo-2.0.tar.gz",
    ],
)
def test_non_sdist_or_mismatching_identity(tmp_path: Path, filename: str) -> None:
    (tmp_path / "PKG-INFO").write_text(
        "Metadata-Version: 2.2\nName: demo\nVersion: 1.0\n"
    )
    assert read_sdist_metadata(str(tmp_path), filename, SDIST_RESOLUTION_FIELDS) is None


@pytest.mark.parametrize(
    "filename, name",
    [
        ("Demo-1.0.tar.gz", "demo"),
        ("demo.package-1.0.tar.gz", "demo.package"),
        ("demo-v1.0.tar.gz", "demo"),
    ],
)
def test_header_backed_metadata_accepts_historical_names(
    tmp_path: Path, filename: str, name: str
) -> None:
    assert parse_sdist_identity(filename) is None
    (tmp_path / "PKG-INFO").write_text(
        f"Metadata-Version: 2.2\nName: {name}\nVersion: 1.0\n"
    )
    dist = read_sdist_metadata(str(tmp_path), filename, SDIST_RESOLUTION_FIELDS)
    assert dist is not None
    assert dist.raw_name == name


@pytest.mark.parametrize("required_fields", [SDIST_RESOLUTION_FIELDS, frozenset({"*"})])
def test_invalid_utf8_sdist_metadata_falls_back(
    tmp_path: Path, required_fields: frozenset[str]
) -> None:
    (tmp_path / "PKG-INFO").write_bytes(
        b"Metadata-Version: 2.2\nName: demo\nVersion: 1.0\nSummary: bad\xff\n"
    )
    assert (
        read_sdist_metadata(str(tmp_path), "demo-1.0.tar.gz", required_fields) is None
    )


def test_unrelated_dynamic_metadata(tmp_path: Path) -> None:
    (tmp_path / "PKG-INFO").write_text(
        "Metadata-Version: 2.2\nName: demo\nVersion: 1.0\n"
        "Dynamic: Summary\nSummary: provisional\n"
        "Dynamic: Description\n\nProvisional description."
    )
    dist = read_sdist_metadata(
        str(tmp_path), "demo-1.0.tar.gz", SDIST_RESOLUTION_FIELDS
    )
    assert dist is not None
    assert "Summary" not in dist.metadata
    assert dist.metadata.get_payload() == ""
    assert dist.metadata.get_all("Dynamic") == ["Summary", "Description"]
    assert (
        read_sdist_metadata(str(tmp_path), "demo-1.0.tar.gz", frozenset({"*"})) is None
    )


def test_complete_static_metadata_preserves_unicode(tmp_path: Path) -> None:
    (tmp_path / "PKG-INFO").write_text(
        "Metadata-Version: 2.2\nName: demo\nVersion: 1.0\nSummary: café\n\n中文",
        encoding="utf-8",
    )
    dist = read_sdist_metadata(str(tmp_path), "demo-1.0.tar.gz", frozenset({"*"}))
    assert dist is not None
    assert str(dist.metadata["Summary"]) == "café"
    assert dist.metadata_dict["description"] == "中文"


def test_complete_metadata_rejects_duplicate_fields(tmp_path: Path) -> None:
    (tmp_path / "PKG-INFO").write_text(
        "Metadata-Version: 2.2\nName: demo\nVersion: 1.0\n"
        "Requires-Python: >=3.10\nRequires-Python: >=3.12\n"
    )
    assert (
        read_sdist_metadata(str(tmp_path), "demo-1.0.tar.gz", frozenset({"*"})) is None
    )


@pytest.mark.parametrize("metadata_version", ["2.2", "2.5", "2.6"])
def test_dynamic_resolution_prefixes(tmp_path: Path, metadata_version: str) -> None:
    (tmp_path / "PKG-INFO").write_text(
        f"Metadata-Version: {metadata_version}\nName: demo\nVersion: 1.0\n"
        "Dynamic: Requires-Dist\nRequires-Dist: dependency==1\n"
        "Dynamic: Provides-Extra\nProvides-Extra: speed\n"
        "Dynamic: Requires-Python\nRequires-Python: >=99\n"
    )
    dist = read_sdist_metadata(str(tmp_path), "demo-1.0.tar.gz", frozenset())
    assert dist is not None
    if metadata_version == "2.6":
        assert list(dist.iter_raw_dependencies()) == ["dependency==1"]
        assert list(dist.iter_provided_extras()) == ["speed"]
    else:
        assert list(dist.iter_raw_dependencies()) == []
        assert list(dist.iter_provided_extras()) == []
    assert "Requires-Python" not in dist.metadata


def test_dynamic_metadata_preserves_static_headers(tmp_path: Path) -> None:
    url = "file:///" + "long-path/" * 20 + "dependency-1.0.tar.gz"
    (tmp_path / "PKG-INFO").write_text(
        "Metadata-Version: 2.2\nName: demo\nVersion: 1.0\n"
        f"Requires-Dist: dependency @ {url}\n"
        "Summary: café\nDynamic: Description\n\n中文",
        encoding="utf-8",
    )
    dist = read_sdist_metadata(
        str(tmp_path), "demo-1.0.tar.gz", SDIST_RESOLUTION_FIELDS
    )
    assert dist is not None
    assert next(iter(dist.iter_dependencies())).url == url
    assert str(dist.metadata["Summary"]) == "café"
    assert dist.metadata.get_payload() == ""
