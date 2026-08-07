"""Which version the adapter tries first.

The universe has to stay ordered by version, because that is what makes
range widening sound, so every preference pip expresses *between* versions
has to be applied where the engine asks for one instead. ``--prefer-binary``
is such a preference: ``CandidateEvaluator._sort_key`` puts
``binary_preference`` above the version, so pip takes a 0.8 wheel over a 1.0
source archive.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pip._vendor.packaging.ranges import VersionRange
from pip._vendor.packaging.specifiers import SpecifierSet
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import Version

from pip._internal.resolution.nab.candidates import CandidateMetadata, HostCandidate
from pip._internal.resolution.nab.engine import PipProvider, YankPolicy
from pip._internal.resolution.nab.inputs import ResolveInputs

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pip._vendor.packaging.utils import NormalizedName

NAME = canonicalize_name("source")


class FakeIndex:
    """``PipHostIndex`` reduced to what choosing a version reads.

    ``versions`` maps a version to whether it needs no build, and it is
    given oldest first because that is the order the real universe is in.
    Every version's metadata reads, so nothing but the ordering decides
    which one comes back.
    """

    def __init__(
        self,
        versions: dict[str, bool],
        *,
        prefer_binary: bool = False,
        installed: str | None = None,
    ) -> None:
        self.universe = tuple(
            HostCandidate(
                project_name=NAME,
                version=Version(version),
                yanked=False,
                is_binary=is_binary,
            )
            for version, is_binary in versions.items()
        )
        self.prefer_binary = prefer_binary
        self.installed = None if installed is None else Version(installed)

    def candidates(self, project_name: NormalizedName) -> Sequence[HostCandidate]:
        return self.universe if project_name == NAME else ()

    def preferred_version(self, project_name: NormalizedName) -> Version | None:
        return self.installed

    def allows_prereleases(self, project_name: NormalizedName) -> bool | None:
        return None

    def prefers_binary(self) -> bool:
        return self.prefer_binary

    def metadata(self, candidate: HostCandidate) -> CandidateMetadata:
        return CandidateMetadata(
            project_name=candidate.project_name,
            version=candidate.version,
            requires_python=None,
            raw_dependencies=(),
            provided_extras=frozenset(),
        )


def _provider(index: FakeIndex) -> PipProvider:
    return PipProvider(
        index=index,  # type: ignore[arg-type]
        inputs=ResolveInputs(),
        constraints={},
        reporter=None,  # type: ignore[arg-type]
        yank_policy=YankPolicy(frozenset()),
        python_version=Version("3.12"),
        ignore_requires_python=False,
        widening=True,
    )


def _range(specifier: str = "") -> VersionRange:
    return SpecifierSet(specifier).to_range()


def test_the_highest_version_wins_without_the_flag() -> None:
    provider = _provider(FakeIndex({"0.8": True, "1.0": False}))
    assert provider.choose_version("source", _range()) == Version("1.0")


def test_prefer_binary_takes_a_lower_wheel_over_a_higher_source_archive() -> None:
    """pip's own answer: ``binary_preference`` outranks the version."""
    provider = _provider(FakeIndex({"0.8": True, "1.0": False}, prefer_binary=True))
    assert provider.choose_version("source", _range()) == Version("0.8")


def test_prefer_binary_still_takes_the_highest_wheel() -> None:
    """A preference between formats, not a licence to stop climbing."""
    index = FakeIndex({"0.8": True, "0.9": True, "1.0": False}, prefer_binary=True)
    assert _provider(index).choose_version("source", _range()) == Version("0.9")


def test_prefer_binary_falls_back_to_a_source_archive() -> None:
    """It is a preference, so a range with no wheel in it still resolves."""
    index = FakeIndex({"0.8": True, "1.0": False}, prefer_binary=True)
    assert _provider(index).choose_version("source", _range(">0.8")) == Version("1.0")


def test_prefer_binary_does_not_outrank_what_is_installed() -> None:
    """An installed version pip may not upgrade past is still taken first."""
    index = FakeIndex({"0.8": True, "1.0": False}, prefer_binary=True, installed="1.0")
    assert _provider(index).choose_version("source", _range()) == Version("1.0")


def test_prefer_binary_leaves_the_universe_ordered_by_version() -> None:
    """Widening reads the universe as a version-ordered list.

    ``widen_decision`` stands a decision in for its neighbours, so the
    neighbours have to be the adjacent *versions*. If the format preference
    had been applied to the universe instead of to the choice, 1.0's lower
    neighbour would be the 0.8 wheel rather than the 0.9 archive.
    """
    index = FakeIndex({"0.8": True, "0.9": False, "1.0": True}, prefer_binary=True)
    provider = _provider(index)
    assert provider.choose_version("source", _range()) == Version("1.0")

    widened = provider.widen_decision("source", Version("1.0"))
    assert widened is not None
    assert Version("0.9") not in widened
    assert Version("1.0") in widened
