import gc
import importlib.metadata
import weakref
from pathlib import PurePosixPath

import pytest

from pip._internal.metadata.importlib._dists import Distribution, WheelDistribution


class _ReadingWheel(WheelDistribution):
    """Track parsed metadata without keeping it alive."""

    def __init__(self, metadata: bytes) -> None:
        super().__init__(
            {PurePosixPath("METADATA"): metadata}, PurePosixPath("demo-1.dist-info")
        )
        self.metadata_refs: list[weakref.ReferenceType[object]] = []

    @property
    def metadata(self) -> importlib.metadata.PackageMetadata:
        metadata = super().metadata
        self.metadata_refs.append(weakref.ref(metadata))
        return metadata


def test_wheel_identity_shares_a_parse_without_retaining_description() -> None:
    wheel = _ReadingWheel(b"Name: demo\nVersion: 1\n\nDescription\n")
    assert (wheel.name, wheel.version) == ("demo", "1")
    assert (wheel.name, wheel.version) == ("demo", "1")
    assert len(wheel.metadata_refs) == 1
    gc.collect()
    assert wheel.metadata_refs[0]() is None

    dist = Distribution(wheel, wheel.info_location, None)
    assert dist.metadata_dict["description"] == "Description\n"


@pytest.mark.parametrize(
    "data",
    [b"Name: demo\n", b"Version: 1\n", b"Name: \nVersion: \n"],
)
def test_incomplete_identity_matches_stdlib(data: bytes) -> None:
    wheel = _ReadingWheel(data)
    original = super(WheelDistribution, wheel)
    for field in ("name", "version"):
        try:
            expected = getattr(original, field)
        except KeyError:
            with pytest.raises(KeyError):
                getattr(wheel, field)
        else:
            assert getattr(wheel, field) == expected
