"""Keep exact version labels and installer sources in candidate constraints."""

from __future__ import annotations

import typing
from functools import total_ordering
from typing import TYPE_CHECKING

from pip._vendor.nab_resolver.keyed_ranges import KeyedRange
from pip._vendor.nab_resolver.values import FrozenValue
from pip._vendor.packaging.ranges import VersionRange
from pip._vendor.packaging.specifiers import SpecifierSet

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from typing_extensions import override

    from pip._vendor.nab_resolver.types import RangeProtocol
    from pip._vendor.packaging.version import Version
else:
    override = getattr(typing, "override", lambda method: method)

__all__ = ["CandidateKey", "CandidateRange"]

_CANDIDATE_VERSIONS = VersionRange.full(admit_arbitrary=False)


@total_ordering
class CandidateKey(FrozenValue):
    """Identify a distribution by exact version text and installer source."""

    __slots__ = __match_args__ = ("version", "source")

    version: Version
    source: str

    def __init__(self, version: Version, source: str) -> None:
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "source", source)

    @override
    def identity(self) -> tuple[Version, str, str]:
        """Order numerically, then distinguish sources and exact version text."""
        return self.version, self.source, str(self.version)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, CandidateKey) or type(other) is not type(self):
            return NotImplemented

        return self.identity() < other.identity()

    @override
    def __str__(self) -> str:
        return str(self.version)


class CandidateRange(KeyedRange[str, "Version"]):
    """Apply PEP 440 bounds independently to each installer source."""

    __slots__ = ()
    __match_args__ = ("default", "overrides", "_hash")

    def __init__(
        self,
        default: RangeProtocol[Version],
        overrides: (
            Mapping[str, RangeProtocol[Version]]
            | Iterable[tuple[str, RangeProtocol[Version]]]
        ) = (),
    ) -> None:
        # Pip prepares only valid PEP 440 versions, including for exact labels.
        super().__init__(
            default & _CANDIDATE_VERSIONS,
            (
                (source, value & _CANDIDATE_VERSIONS)
                for source, value in dict(overrides).items()
            ),
        )

    @classmethod
    def empty(cls) -> CandidateRange:
        return cls(VersionRange.empty())

    @classmethod
    def full(cls) -> CandidateRange:
        return cls(_CANDIDATE_VERSIONS)

    @classmethod
    def singleton(cls, version: CandidateKey) -> CandidateRange:
        """Retain the exact version label as well as its source."""
        return cls.for_source(
            version.source, SpecifierSet(f"==={version.version}").to_range()
        )

    @classmethod
    def for_source(
        cls, source: str, versions: RangeProtocol[Version] | None = None
    ) -> CandidateRange:
        if versions is None:
            versions = _CANDIDATE_VERSIONS

        return cls(VersionRange.empty(), [(source, versions)])

    def for_versions(self, source: str) -> RangeProtocol[Version]:
        return self.for_key(source)

    def __contains__(self, candidate: CandidateKey) -> bool:
        return self.contains(candidate.source, candidate.version)

    @override
    def __str__(self) -> str:
        """Keep source coordinates ordered in installer diagnostics."""
        return f"{self.default!r}; sources={tuple(sorted(self.overrides))!r}"
