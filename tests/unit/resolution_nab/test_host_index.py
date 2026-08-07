"""The candidate supplier driven by a real ``PackageFinder``.

``test_candidates.py`` checks the rules in isolation. These run the whole
pip side of the adapter against pip's own test data, which is the only way
to catch a wiring mistake between the finder, the factory and the universe.
"""

from __future__ import annotations

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
    inputs = collect_inputs(
        [install_req_from_line(line) for line in lines],
        ignore_dependencies=False,
    )
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

    metadata = index.metadata(universe[-1])
    assert metadata.project_name == canonicalize_name("requires-simple-extra")
    assert any("extra ==" in dep for dep in metadata.raw_dependencies)
    assert metadata.provided_extras == frozenset({canonicalize_name("extra")})


def test_the_universe_records_which_versions_need_no_build(
    factory: Factory, yanking_finder: PackageFinder
) -> None:
    """``--prefer-binary`` sorts on this, so it has to survive the grouping."""
    index = _index(factory, yanking_finder, ["simplewheel", "simple"])

    wheels = index.candidates(canonicalize_name("simplewheel"))
    assert wheels
    assert all(candidate.is_binary for candidate in wheels)

    archives = index.candidates(canonicalize_name("simple"))
    assert archives
    assert not any(candidate.is_binary for candidate in archives)
