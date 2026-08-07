"""The candidate universe the nab adapter hands the engine.

Three properties are correctness, not performance: the universe contains
every selectable version, it is ordered by version, and yanked versions are
carried and flagged rather than dropped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pip._vendor.packaging.specifiers import SpecifierSet
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import Version

from pip._internal.index.package_finder import CandidateEvaluator
from pip._internal.models.candidate import InstallationCandidate
from pip._internal.models.link import Link
from pip._internal.models.release_control import ReleaseControl
from pip._internal.resolution.nab.candidates import HostCandidate
from pip._internal.resolution.nab.engine import YankPolicy
from pip._internal.resolution.nab.inputs import is_pinned, split_key

from tests.lib.index import make_mock_candidate

if TYPE_CHECKING:
    from pip._vendor.packaging.utils import NormalizedName


def _evaluator(release_control: ReleaseControl | None = None) -> CandidateEvaluator:
    return CandidateEvaluator.create(
        project_name="mypackage",
        release_control=release_control,
    )


def _universe(
    evaluator: CandidateEvaluator, candidates: list[InstallationCandidate]
) -> list[tuple[str, str, bool]]:
    """Version-ordered records, exactly as ``PipHostIndex`` builds them."""
    ranked = evaluator.rank_candidates(candidates)
    best_by_version = {candidate.version: candidate for candidate in ranked}
    return [
        (
            str(version),
            best_by_version[version].link.filename,
            best_by_version[version].link.is_yanked,
        )
        for version in sorted(best_by_version)
    ]


def test_rank_candidates_is_not_version_ordered_but_grouping_fixes_it() -> None:
    """``_sort_key`` leads with yank, so its own order is not monotonic."""
    evaluator = _evaluator()
    candidates = [
        make_mock_candidate("1.0"),
        make_mock_candidate("2.0", yanked_reason="bad"),
        make_mock_candidate("3.0"),
    ]

    ranked = evaluator.rank_candidates(candidates)
    assert [str(c.version) for c in ranked] == ["2.0", "1.0", "3.0"]

    assert [version for version, _, _ in _universe(evaluator, candidates)] == [
        "1.0",
        "2.0",
        "3.0",
    ]


def test_universe_keeps_yanked_versions_and_flags_them() -> None:
    evaluator = _evaluator()
    candidates = [
        make_mock_candidate("1.0"),
        make_mock_candidate("2.0", yanked_reason="please stop"),
    ]

    assert [
        (version, yanked) for version, _, yanked in _universe(evaluator, candidates)
    ] == [("1.0", False), ("2.0", True)]


def test_universe_takes_pips_preferred_file_for_each_version() -> None:
    """One record per version, representing the file pip would pick."""
    evaluator = _evaluator()
    sdist = InstallationCandidate(
        "mypackage", "1.0", Link("https://example.com/mypackage-1.0.tar.gz")
    )
    wheel = InstallationCandidate(
        "mypackage",
        "1.0",
        Link("https://example.com/mypackage-1.0-py3-none-any.whl"),
    )

    universe = _universe(evaluator, [sdist, wheel])
    assert len(universe) == 1
    assert universe[0][1] == "mypackage-1.0-py3-none-any.whl"


def test_rank_candidates_does_not_hide_prereleases() -> None:
    """The prerelease trap.

    ``get_applicable_candidates`` runs the candidates through a
    ``SpecifierSet``, and an empty one applies PEP 440's default of hiding
    prereleases. A dependency on ``>=1.0b1`` would then be unsatisfiable, so
    the adapter must never let a specifier reach pip's candidate path.
    """
    candidates = [make_mock_candidate("1.0b1"), make_mock_candidate("0.9")]
    evaluator = _evaluator()

    assert [
        str(c.version) for c in evaluator.get_applicable_candidates(candidates)
    ] == ["0.9"]
    assert [str(c.version) for c in evaluator.rank_candidates(candidates)] == [
        "0.9",
        "1.0b1",
    ]


def test_rank_candidates_applies_only_final() -> None:
    """``--only-final`` is not applied by ``find_all_candidates``."""
    release_control = ReleaseControl()
    release_control.handle_mutual_excludes(
        ":all:", release_control.only_final, release_control.all_releases, "only_final"
    )
    evaluator = _evaluator(release_control)

    candidates = [make_mock_candidate("1.0b1"), make_mock_candidate("0.9")]
    assert [str(c.version) for c in evaluator.rank_candidates(candidates)] == ["0.9"]


def test_rank_candidates_keeps_prereleases_under_all_releases() -> None:
    release_control = ReleaseControl()
    release_control.handle_mutual_excludes(
        ":all:",
        release_control.all_releases,
        release_control.only_final,
        "all_releases",
    )
    evaluator = _evaluator(release_control)

    candidates = [make_mock_candidate("1.0b1"), make_mock_candidate("0.9")]
    assert [str(c.version) for c in evaluator.rank_candidates(candidates)] == [
        "0.9",
        "1.0b1",
    ]


@pytest.mark.parametrize(
    "specifier, expected",
    [
        ("", False),
        (">=1.0", False),
        ("==1.0", True),
        ("===1.0", True),
        ("==1.*", False),
        (">=1.0,==1.5", True),
        ("!=1.0", False),
        ("~=1.0", False),
    ],
)
def test_is_pinned_matches_pips_rule(specifier: str, expected: bool) -> None:
    assert is_pinned(SpecifierSet(specifier)) is expected


def test_yank_policy_reports_facts_and_leaves_the_rule_to_nab() -> None:
    """pip answers "is it yanked" and "is it pinned"; nab combines them."""
    universe = {
        canonicalize_name("mypackage"): [
            _record("1.0", yanked=False),
            _record("2.0", yanked=True),
        ]
    }
    policy = YankPolicy(
        _FixedIndex(universe), frozenset({canonicalize_name("mypackage")})
    )
    name = canonicalize_name("mypackage")

    assert policy.yanked_versions(name, [Version("1.0"), Version("2.0")]) == frozenset(
        {Version("2.0")}
    )
    assert not policy.admits_yanked(name, all_yanked=False)
    assert policy.admits_yanked(name, all_yanked=True)


def test_yank_policy_refuses_a_package_the_command_line_does_not_pin() -> None:
    """The command line pins under-approximate: a transitive == is missed."""
    policy = YankPolicy(_FixedIndex({}), frozenset())

    assert not policy.admits_yanked(canonicalize_name("mypackage"), all_yanked=True)


def _record(version: str, *, yanked: bool) -> HostCandidate:
    return HostCandidate(
        project_name=canonicalize_name("mypackage"),
        version=Version(version),
        yanked=yanked,
    )


class _FixedIndex:
    """``PipHostIndex.yanked_versions`` over a fixed universe."""

    def __init__(self, universe: dict[NormalizedName, list[HostCandidate]]) -> None:
        self._universe = universe

    def yanked_versions(self, project_name: NormalizedName) -> frozenset[Version]:
        return frozenset(
            candidate.version
            for candidate in self._universe.get(project_name, [])
            if candidate.yanked
        )


@pytest.mark.parametrize(
    "key, expected",
    [
        ("foo", ("foo", frozenset())),
        ("Foo_Bar", ("foo-bar", frozenset())),
        ("foo[a,b]", ("foo", frozenset({"a", "b"}))),
        ("foo[A]", ("foo", frozenset({"a"}))),
    ],
)
def test_split_key(key: str, expected: tuple[str, frozenset[str]]) -> None:
    assert split_key(key) == expected
