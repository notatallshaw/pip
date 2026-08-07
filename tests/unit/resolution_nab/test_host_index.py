"""The candidate supplier driven by a real ``PackageFinder``.

``test_candidates.py`` checks the rules in isolation. These run the whole
pip side of the adapter against pip's own test data, which is the only way
to catch a wiring mistake between the finder, the factory and the universe.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from pip._vendor.packaging.utils import canonicalize_name

from pip._internal.index.collector import LinkCollector
from pip._internal.index.package_finder import PackageFinder
from pip._internal.models.search_scope import SearchScope
from pip._internal.models.selection_prefs import SelectionPreferences
from pip._internal.network.session import PipSession
from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.nab.candidates import PipHostIndex
from pip._internal.resolution.nab.inputs import collect_inputs

if TYPE_CHECKING:
    from pip._internal.req.req_install import InstallRequirement
    from pip._internal.resolution.model.factory import Factory

    from tests.lib import TestData


@pytest.fixture
def yanking_finder(data: TestData) -> PackageFinder:
    """A finder configured the way ``pip install`` configures one.

    ``allow_yanked=True`` is what makes pip's candidate list a legal universe
    for range widening: nothing selectable is removed before the resolver
    sees it.
    """
    collector = LinkCollector(
        PipSession(), SearchScope([str(data.packages)], [], False)
    )
    return PackageFinder.create(collector, SelectionPreferences(allow_yanked=True))


def _index(factory: Factory, finder: PackageFinder, lines: list[str]) -> PipHostIndex:
    return _index_from_ireqs(
        factory, finder, [install_req_from_line(line) for line in lines]
    )


def _index_from_ireqs(
    factory: Factory, finder: PackageFinder, ireqs: list[InstallRequirement]
) -> PipHostIndex:
    inputs = collect_inputs(ireqs, ignore_dependencies=False)
    return PipHostIndex(
        factory=factory,
        finder=finder,
        inputs=inputs,
        upgrade_strategy="to-satisfy-only",
        make_install_req=install_req_from_line,
    )


def test_universe_is_ascending_and_deduplicated(
    factory: Factory, yanking_finder: PackageFinder
) -> None:
    index = _index(factory, yanking_finder, ["simple"])
    universe = index.candidates(canonicalize_name("simple"))

    versions = [str(candidate.version) for candidate in universe]
    assert versions == sorted(versions, key=lambda v: [int(p) for p in v.split(".")])
    assert len(versions) == len(set(versions))
    assert "3.0" in versions


def test_universe_is_memoized(factory: Factory, yanking_finder: PackageFinder) -> None:
    index = _index(factory, yanking_finder, ["simple"])
    name = canonicalize_name("simple")
    assert index.candidates(name) is index.candidates(name)


def test_unknown_project_gives_an_empty_universe(
    factory: Factory, yanking_finder: PackageFinder
) -> None:
    index = _index(factory, yanking_finder, ["simple"])
    assert index.candidates(canonicalize_name("no-such-project-at-all")) == ()


def test_metadata_reads_raw_dependencies(
    factory: Factory, yanking_finder: PackageFinder
) -> None:
    """Raw, not environment-evaluated.

    ``BaseDistribution.iter_dependencies`` drops every ``; extra == "x"``
    line and pre-consumes environment markers, which would make extras
    unresolvable.
    """
    index = _index(factory, yanking_finder, ["requires-simple-extra"])
    universe = index.candidates(canonicalize_name("requires-simple-extra"))
    assert universe

    dist = index.metadata(universe[-1])
    assert canonicalize_name(dist.raw_name) == canonicalize_name(
        "requires-simple-extra"
    )
    assert any("extra ==" in dep for dep in dist.iter_raw_dependencies())
    assert set(dist.iter_provided_extras()) == {canonicalize_name("extra")}


def test_the_universe_records_which_versions_need_no_build(
    factory: Factory, yanking_finder: PackageFinder
) -> None:
    """``--prefer-binary`` sorts on this, so it has to survive the grouping."""
    index = _index(factory, yanking_finder, ["simplewheel", "simple"])

    wheels = index.candidates(canonicalize_name("simplewheel"))
    assert wheels
    assert index.binary_versions(canonicalize_name("simplewheel")) == {
        candidate.version for candidate in wheels
    }

    archives = index.candidates(canonicalize_name("simple"))
    assert archives
    assert not index.binary_versions(canonicalize_name("simple"))


def test_a_constraints_hash_pins_the_candidates_install_requirement(
    factory: Factory, yanking_finder: PackageFinder, data: TestData
) -> None:
    """A constraint may carry the ``--hash`` lines for an unpinned requirement.

    pip copies them onto the template it builds candidates from, and
    ``make_install_req_from_link`` then writes the candidate's install
    requirement as ``name==version`` rather than repeating the unpinned
    requirement. Without that the install fails hash checking with "all
    requirements must have their versions pinned with ==".
    """
    wheel = data.packages / "simplewheel-1.0-py2.py3-none-any.whl"
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    index = _index_from_ireqs(
        factory,
        yanking_finder,
        [
            install_req_from_line("simplewheel"),
            install_req_from_line(
                "simplewheel==1.0",
                constraint=True,
                hash_options={"sha256": [digest]},
            ),
        ],
    )

    name = canonicalize_name("simplewheel")
    chosen = next(
        candidate
        for candidate in index.candidates(name)
        if str(candidate.version) == "1.0"
    )
    ireq = index.pip_candidate(chosen, frozenset()).get_install_requirement()

    assert ireq is not None
    assert str(ireq.req) == "simplewheel==1.0"
    assert ireq.is_pinned
    assert ireq.hash_options == {"sha256": [digest]}
