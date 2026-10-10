"""Keep source location restrictions when checksums differ."""

import pytest

from pip._internal.models.link import Link
from pip._internal.resolution.nab.base import source_links_equivalent


@pytest.mark.parametrize(
    "left,right,equivalent",
    [
        ("file:///tmp/pkg.whl", "file://localhost/tmp/pkg.whl#sha256=abc", True),
        (
            "https://example.com/pkg.whl#sha256=abc",
            "https://example.com/pkg.whl#sha512=def",
            True,
        ),
        (
            "https://example.com/pkg.whl#sha256=abc&sha512=def",
            "https://example.com/pkg.whl",
            True,
        ),
        (
            "https://example.com/pkg.whl?a=1",
            "https://example.com/pkg.whl?a=2#sha256=abc",
            False,
        ),
        (
            "https://example.com/pkg.tar.gz#subdirectory=a",
            "https://example.com/pkg.tar.gz#subdirectory=b&sha256=abc",
            False,
        ),
        (
            "https://example.com/pkg.tar.gz#subdirectory=a",
            "https://example.com/pkg.tar.gz#sha256=abc&subdirectory=a",
            True,
        ),
        (
            "https://example.com/pkg.whl",
            "https://example.com/other.whl#sha256=abc",
            False,
        ),
    ],
)
def test_source_location_survives_checksum_normalization(
    left: str, right: str, *, equivalent: bool
) -> None:
    assert source_links_equivalent(Link(left), Link(right)) is equivalent
