import gc
import importlib.metadata
import weakref
from pathlib import PurePosixPath

import pytest

from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import Version

from pip._internal.exceptions import MetadataInconsistent, UnsupportedWheel
from pip._internal.metadata.importlib._dists import Distribution, WheelDistribution
from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.resolvelib.candidates import LinkCandidate


class _ReadingWheel(WheelDistribution):
    """Track metadata reads and parsed-message lifetimes."""

    def __init__(self, metadata: bytes) -> None:
        super().__init__(
            {PurePosixPath("METADATA"): metadata}, PurePosixPath("demo-1.dist-info")
        )
        self.metadata_requests = 0
        self.metadata_refs: list[weakref.ReferenceType[object]] = []

    @property
    def metadata(self) -> importlib.metadata.PackageMetadata:
        metadata = super().metadata
        self.metadata_refs.append(weakref.ref(metadata))
        return metadata

    def read_text(self, filename: str) -> str | None:
        if filename == "METADATA":
            self.metadata_requests += 1
        return super().read_text(filename)


def test_candidate_fields_share_identity_without_retaining_description() -> None:
    wheel = _ReadingWheel(
        b"Metadata-Version: 2.1\nName: Demo\nVersion: 1\n"
        b"Requires-Dist: dep>=1\n\nDescription\n"
    )
    dist = Distribution(wheel, wheel.info_location, None)

    assert dist.canonical_name == "demo"
    assert str(dist.version) == "1"
    assert wheel.metadata_requests == 1
    gc.collect()
    assert all(ref() is None for ref in wheel.metadata_refs)

    assert [str(req) for req in dist.iter_dependencies()] == ["dep>=1"]
    assert dist.metadata_dict["description"] == "Description\n"
    assert wheel.metadata_requests == 2
    assert dist.read_text("METADATA").endswith("\n\nDescription\n")


def test_name_rejection_keeps_only_identity() -> None:
    wheel = _ReadingWheel(b"Name: demo\nVersion: 1\n\nDescription\n")
    dist = Distribution(wheel, wheel.info_location, None)
    candidate = object.__new__(LinkCandidate)
    candidate._name = canonicalize_name("other")
    candidate._version = None
    candidate._ireq = install_req_from_line("other==1")

    with pytest.raises(MetadataInconsistent) as failure:
        candidate._check_metadata_consistency(dist)

    assert failure.value.field == "name"
    assert wheel.metadata_requests == 1
    gc.collect()
    assert all(ref() is None for ref in wheel.metadata_refs)


def test_version_rejection_releases_parsed_description() -> None:
    wheel = _ReadingWheel(b"Name: demo\nVersion: 1\n\nDescription\n")
    dist = Distribution(wheel, wheel.info_location, None)
    candidate = object.__new__(LinkCandidate)
    candidate._name = canonicalize_name("demo")
    candidate._version = Version("2")
    candidate._ireq = install_req_from_line("demo==2")

    with pytest.raises(MetadataInconsistent) as failure:
        candidate._check_metadata_consistency(dist)

    assert failure.value.field == "version"
    assert wheel.metadata_requests == 1
    gc.collect()
    assert all(ref() is None for ref in wheel.metadata_refs)


@pytest.mark.parametrize(
    "data",
    [b"", b"Name: demo\n", b"Version: 1\n", b"Name: \nVersion: \n"],
)
@pytest.mark.parametrize("field", ["name", "version"])
def test_missing_identity_matches_stdlib(data: bytes, field: str) -> None:
    wheel = _ReadingWheel(data)
    original = super(WheelDistribution, wheel)
    try:
        expected = getattr(original, field)
    except (KeyError, TypeError, FileNotFoundError) as error:
        with pytest.raises(type(error)):
            getattr(wheel, field)
    else:
        assert getattr(wheel, field) == expected


def test_invalid_utf8_does_not_cache_failed_identity() -> None:
    wheel = _ReadingWheel(b"Name: demo\nVersion: 1\n\n\xff")
    for _ in range(2):
        with pytest.raises(UnsupportedWheel):
            _ = wheel.name
    assert wheel.metadata_requests == 2


def test_metadata_getter_is_still_uncached() -> None:
    wheel = _ReadingWheel(b"Name: demo\nVersion: 1\n\nDescription\n")
    assert isinstance(wheel, importlib.metadata.Distribution)
    first = wheel.metadata
    second = wheel.metadata
    assert first is not second
    assert wheel.metadata_requests == 2
