"""Keep source location restrictions when checksums differ."""

import hashlib

import pytest

from pip._internal.exceptions import HashMismatch
from pip._internal.models.link import Link
from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.nab.base import Constraint, source_links_equivalent
from pip._internal.utils.hashes import Hashes


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


@pytest.mark.parametrize("algorithm", ["sha256", "sha512"])
def test_disjoint_source_constraints_refuse_the_archive(algorithm: str) -> None:
    content = b"package archive"
    valid = hashlib.sha256(content).hexdigest()
    root = install_req_from_line("dep", hash_options={"sha256": [valid]})
    other = "0" * 64 if algorithm == "sha256" else hashlib.sha512(content).hexdigest()
    constraint = install_req_from_line("dep", hash_options={algorithm: [other]})

    combined = Constraint.from_ireq(constraint) & root & root

    with pytest.raises(HashMismatch):
        combined.hashes.check_against_chunks([content])
    with pytest.raises(HashMismatch):
        Hashes(combined.hash_options).check_against_chunks([content])
