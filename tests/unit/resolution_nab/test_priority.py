"""The decision priority key the adapter hands nab.

``prioritize`` is called once per undecided package per decision round, so
its candidate count is asked for hundreds of thousands of times on a hard
resolve while the thing it counts never changes. These cases pin three
things: the count is memoised, the memo answers exactly what a rescan would
have answered, and ranking a package never buys the listing that would let
it be counted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pip._vendor.packaging.ranges import VersionRange
from pip._vendor.packaging.specifiers import SpecifierSet
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import Version

from pip._internal.resolution.nab.candidates import HostCandidate
from pip._internal.resolution.nab.engine import (
    _NO_LISTING_PRIOR,
    PipProvider,
    YankPolicy,
)
from pip._internal.resolution.nab.inputs import ResolveInputs

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pip._vendor.packaging.utils import NormalizedName


class CountingIndex:
    """A candidate universe that records how often it is asked.

    Stands in for ``PipHostIndex`` at the one boundary these cases care
    about. The universe it returns is the real thing a built one holds: a
    tuple of ``HostCandidate`` with real ``Version`` objects, one per
    version, oldest first. ``is_listed`` moves the way the real one does:
    False until something has asked for the universe, True forever after.
    """

    def __init__(self, universes: dict[str, list[str]]) -> None:
        self.universes = {
            canonicalize_name(name): tuple(
                HostCandidate(
                    project_name=canonicalize_name(name),
                    version=Version(version),
                    yanked=False,
                )
                for version in versions
            )
            for name, versions in universes.items()
        }
        self.calls: list[NormalizedName] = []
        self._listed: set[NormalizedName] = set()

    def candidates(self, project_name: NormalizedName) -> Sequence[HostCandidate]:
        self.calls.append(project_name)
        self._listed.add(project_name)
        return self.universes.get(project_name, ())

    def is_listed(self, project_name: NormalizedName) -> bool:
        return project_name in self._listed

    def pay_for(self, *names: str) -> None:
        """Buy the listings, the way ``choose_version`` does, then start clean."""
        for name in names:
            self.candidates(canonicalize_name(name))
        self.calls.clear()


def _provider(index: CountingIndex) -> PipProvider:
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


def _range(specifier: str) -> VersionRange:
    return SpecifierSet(specifier).to_range()


VERSIONS = ["1.0", "1.1", "2.0", "2.1", "3.0"]


@pytest.mark.parametrize(
    "specifier, expected",
    [
        ("", 5),
        (">=2.0", 3),
        (">=1.1,<3.0", 3),
        ("==2.0", 1),
        (">=9.0", 0),
    ],
)
def test_matching_term_counts_the_universe(specifier: str, expected: int) -> None:
    """The count is what a scan of the universe would produce."""
    index = CountingIndex({"widget": VERSIONS})
    index.pay_for("widget")
    provider = _provider(index)
    _, matching, _ = provider.prioritize("widget", _range(specifier), {})
    assert matching == expected


def test_an_unlisted_project_is_ranked_without_being_listed() -> None:
    """Ranking must not buy an index request. This is the whole point."""
    index = CountingIndex({"widget": VERSIONS})
    provider = _provider(index)
    assert provider.prioritize("widget", _range(""), {}) == (
        1,
        _NO_LISTING_PRIOR,
        True,
    )
    assert index.calls == []


def test_an_unlisted_project_sorts_behind_a_listed_one() -> None:
    """The prior is the ordering rule, not just a placeholder value."""
    index = CountingIndex({"widget": VERSIONS, "gadget": VERSIONS})
    index.pay_for("widget")
    provider = _provider(index)
    listed = provider.prioritize("widget", _range(""), {})
    unlisted = provider.prioritize("gadget", _range(""), {})
    assert listed < unlisted


def test_the_prior_is_not_memoised() -> None:
    """The listing arrives later in the same resolve, so the count must too."""
    index = CountingIndex({"widget": VERSIONS})
    provider = _provider(index)
    version_range = _range(">=2.0")
    assert provider.prioritize("widget", version_range, {})[1] == _NO_LISTING_PRIOR
    index.pay_for("widget")
    assert provider.prioritize("widget", version_range, {})[1] == 3


def test_repeated_calls_ask_the_universe_once() -> None:
    """The rescan is the whole cost, so a repeat must not pay it again."""
    index = CountingIndex({"widget": VERSIONS})
    index.pay_for("widget")
    provider = _provider(index)
    version_range = _range(">=1.1")
    first = provider.prioritize("widget", version_range, {})
    for _ in range(50):
        assert provider.prioritize("widget", version_range, {}) == first
    assert index.calls == [canonicalize_name("widget")]


def test_an_equal_range_hits_the_same_entry() -> None:
    """Equal ranges agree on membership, so one entry serves both."""
    index = CountingIndex({"widget": VERSIONS})
    index.pay_for("widget")
    provider = _provider(index)
    first = provider.prioritize("widget", _range(">=1.1,<3.0"), {})
    second = provider.prioritize("widget", _range(">=1.1,<3.0"), {})
    assert first == second == (1, 3, True)
    assert index.calls == [canonicalize_name("widget")]


def test_each_range_is_counted_on_its_own() -> None:
    """The memo is keyed on the range, not just the project."""
    index = CountingIndex({"widget": VERSIONS})
    index.pay_for("widget")
    provider = _provider(index)
    assert provider.prioritize("widget", _range(">=2.0"), {})[1] == 3
    assert provider.prioritize("widget", _range("==1.0"), {})[1] == 1
    assert provider.prioritize("widget", _range(""), {})[1] == 5
    assert index.calls == [canonicalize_name("widget")] * 3


def test_each_project_is_counted_on_its_own() -> None:
    """Two projects that share a range do not share a count."""
    index = CountingIndex({"widget": VERSIONS, "gadget": ["1.0"]})
    index.pay_for("widget", "gadget")
    provider = _provider(index)
    version_range = _range(">=1.0")
    assert provider.prioritize("widget", version_range, {})[1] == 5
    assert provider.prioritize("gadget", version_range, {})[1] == 1


def test_an_extras_node_shares_its_base_count() -> None:
    """An extras node ranges over the base's versions, and sorts first."""
    index = CountingIndex({"widget": VERSIONS})
    index.pay_for("widget")
    provider = _provider(index)
    version_range = _range(">=2.0")
    base = provider.prioritize("widget", version_range, {})
    node = provider.prioritize("widget[extra]", version_range, {})
    assert base == (1, 3, True)
    assert node == (1, 3, False)
    assert index.calls == [canonicalize_name("widget")]


def test_a_missing_universe_is_not_cached_as_a_count() -> None:
    """An empty universe is a real answer of zero, and stays memoised."""
    index = CountingIndex({})
    index.pay_for("widget")
    provider = _provider(index)
    assert provider.prioritize("widget", _range(""), {})[1] == 0
    assert provider.prioritize("widget", _range(""), {})[1] == 0
    assert index.calls == [canonicalize_name("widget")]


def test_the_universe_build_still_happens_on_the_first_ask() -> None:
    """A memo hit implies an earlier miss, so nothing skips the build."""
    index = CountingIndex({"widget": VERSIONS, "gadget": ["1.0", "2.0"]})
    index.pay_for("widget", "gadget")
    provider = _provider(index)
    provider.prioritize("widget", _range(""), {})
    assert index.calls == [canonicalize_name("widget")]
    provider.prioritize("gadget", _range(""), {})
    assert index.calls == [canonicalize_name("widget"), canonicalize_name("gadget")]


def test_the_tier_still_moves_with_the_conflict_count() -> None:
    """The memo must not freeze the part of the key that does change."""
    index = CountingIndex({"widget": VERSIONS})
    index.pay_for("widget")
    provider = _provider(index)
    version_range = _range("")
    assert provider.prioritize("widget", version_range, {})[0] == 1
    assert provider.prioritize("widget", version_range, {"widget": 5})[0] == 0
    assert provider.prioritize("widget", version_range, {"widget": 4})[0] == 1
    assert provider.prioritize("widget", version_range, {}, {"widget": 9})[0] == 2
