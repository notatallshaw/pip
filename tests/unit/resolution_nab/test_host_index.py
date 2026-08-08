"""The candidate supplier driven by a real ``PackageFinder``.

``test_candidates.py`` checks the rules in isolation. These run the whole
pip side of the adapter against pip's own test data, which is the only way
to catch a wiring mistake between the finder, the factory and the universe.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from pip._vendor.packaging.ranges import VersionRange
from pip._vendor.packaging.specifiers import SpecifierSet
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import Version

from pip._internal.index.collector import LinkCollector
from pip._internal.index.package_finder import PackageFinder
from pip._internal.metadata import BaseDistribution, get_metadata_distribution
from pip._internal.models.search_scope import SearchScope
from pip._internal.models.selection_prefs import SelectionPreferences
from pip._internal.network.session import PipSession
from pip._internal.req.constructors import install_req_from_line
from pip._internal.req.req_install import InstallRequirement
from pip._internal.resolution.nab.candidates import PipHostIndex
from pip._internal.resolution.nab.engine import PipProvider, YankPolicy
from pip._internal.resolution.nab.inputs import ResolveInputs, collect_inputs

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


def test_a_constraints_hash_pins_the_candidates_install_requirement(
    factory: Factory, yanking_finder: PackageFinder, data: TestData
) -> None:
    """Cover for pypa/pip#9243.

    A constraint may carry the ``--hash`` lines for a requirement that has
    none of its own. pip copies them onto the template it builds candidates
    from, and ``make_install_req_from_link`` then writes the candidate's
    install requirement as ``name==version`` rather than repeating the
    unpinned requirement. Without that the install fails hash checking with
    "all requirements must have their versions pinned with ==", which is
    the bug pip fixed.
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


def _installed(name: str, version: str) -> BaseDistribution:
    """A real distribution over real METADATA bytes, not a stand-in.

    ``PipHostIndex`` reads ``.version`` and hands the object to
    ``Factory._make_candidate_from_dist``, so the parsing is part of what
    these cases check.
    """
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    return get_metadata_distribution(
        metadata.encode("utf-8"),
        f"{name}-{version}-py3-none-any.whl",
        canonicalize_name(name),
    )


@pytest.fixture
def blocked_finder(yanking_finder: PackageFinder) -> PackageFinder:
    """A finder that fails the way pip's test suite fails on a request.

    Sockets are blocked in the pip subprocess the functional tests spawn, so
    an index request that is merely slow in production is a hard error
    there. Raising here reproduces that in a unit test.
    """

    def refuse(project_name: str) -> None:
        raise AssertionError(f"listed the index for {project_name}")

    yanking_finder.find_all_candidates = refuse  # type: ignore[assignment]
    return yanking_finder


def _provider(index: PipHostIndex, inputs: ResolveInputs) -> PipProvider:
    return PipProvider(
        index=index,
        inputs=inputs,
        constraints={},
        reporter=None,  # type: ignore[arg-type]
        yank_policy=YankPolicy(frozenset()),
        python_version=Version("3.12"),
        ignore_requires_python=False,
        widening=True,
    )


def test_the_installed_distribution_answers_without_listing(
    factory: Factory, blocked_finder: PackageFinder
) -> None:
    name = canonicalize_name("simple")
    factory._installed_dists = {name: _installed("simple", "1.0")}
    index = _index(factory, blocked_finder, ["simple"])

    record = index.installed(name)
    assert record is not None
    assert str(record.version) == "1.0"
    assert record.is_installed
    assert index.find(name, Version("1.0")) is record
    assert not index.is_listed(name)


def test_a_version_that_is_not_installed_still_reaches_the_index(
    factory: Factory, blocked_finder: PackageFinder
) -> None:
    """The soundness half: the installed record is a prefix, not the universe."""
    name = canonicalize_name("simple")
    factory._installed_dists = {name: _installed("simple", "1.0")}
    index = _index(factory, blocked_finder, ["simple"])

    with pytest.raises(AssertionError, match="listed the index for simple"):
        index.find(name, Version("2.0"))


def test_a_url_requirement_hides_the_installed_distribution(
    factory: Factory, blocked_finder: PackageFinder, data: TestData
) -> None:
    """A package pinned to a link has that link as its whole universe."""
    name = canonicalize_name("simple")
    factory._installed_dists = {name: _installed("simple", "1.0")}
    archive = (data.packages / "simple-1.0.tar.gz").as_uri()
    index = _index(factory, blocked_finder, [f"simple @ {archive}"])

    assert index.installed(name) is None


def test_force_reinstall_sends_the_answer_back_to_the_index(
    factory: Factory, blocked_finder: PackageFinder
) -> None:
    name = canonicalize_name("simple")
    factory._installed_dists = {name: _installed("simple", "1.0")}
    factory._force_reinstall = True
    index = _index(factory, blocked_finder, ["simple"])

    assert index.installed(name) is None


def test_an_already_satisfied_requirement_is_decided_without_listing(
    factory: Factory, blocked_finder: PackageFinder
) -> None:
    """The whole of "Requirement already satisfied", with no request.

    ``choose_version`` and ``get_dependencies`` are the two calls the
    resolver makes for a package it decides in one step, and neither may
    reach the finder.
    """
    name = canonicalize_name("simple")
    factory._installed_dists = {name: _installed("simple", "1.0")}
    ireqs = [install_req_from_line("simple")]
    inputs = collect_inputs(ireqs, ignore_dependencies=False)
    index = PipHostIndex(
        factory=factory,
        finder=blocked_finder,
        inputs=inputs,
        upgrade_strategy="to-satisfy-only",
        make_install_req=install_req_from_line,
    )
    provider = _provider(index, inputs)

    chosen = provider.choose_version("simple", VersionRange.full())
    assert chosen == Version("1.0")
    assert provider.get_dependencies("simple", chosen) == {}
    assert provider.widen_decision("simple", chosen) is None
    assert not index.is_listed(name)


def test_a_bound_the_installed_version_misses_still_reaches_the_index(
    factory: Factory, blocked_finder: PackageFinder
) -> None:
    """Without this the first case would also pass a truncated universe."""
    name = canonicalize_name("simple")
    factory._installed_dists = {name: _installed("simple", "1.0")}
    ireqs = [install_req_from_line("simple>=2")]
    inputs = collect_inputs(ireqs, ignore_dependencies=False)
    index = PipHostIndex(
        factory=factory,
        finder=blocked_finder,
        inputs=inputs,
        upgrade_strategy="to-satisfy-only",
        make_install_req=install_req_from_line,
    )
    provider = _provider(index, inputs)

    with pytest.raises(AssertionError, match="listed the index for simple"):
        provider.choose_version("simple", SpecifierSet(">=2").to_range())


def test_an_upgrade_sends_the_answer_back_to_the_index(
    factory: Factory, blocked_finder: PackageFinder
) -> None:
    """``--upgrade`` is what picks pip's other merge iterator."""
    name = canonicalize_name("simple")
    factory._installed_dists = {name: _installed("simple", "1.0")}
    ireqs = [install_req_from_line("simple")]
    inputs = collect_inputs(ireqs, ignore_dependencies=False)
    index = PipHostIndex(
        factory=factory,
        finder=blocked_finder,
        inputs=inputs,
        upgrade_strategy="eager",
        make_install_req=install_req_from_line,
    )
    provider = _provider(index, inputs)

    with pytest.raises(AssertionError, match="listed the index for simple"):
        provider.choose_version("simple", VersionRange.full())


def test_an_extras_nodes_dependency_is_credited_to_the_extras_requirement(
    factory: Factory, yanking_finder: PackageFinder
) -> None:
    """``(from pkg[ext])``, not ``(from pkg)`` and not nothing at all.

    pip builds its ``ExtrasCandidate`` from the ireq that carried the
    extras and hands that one to every dependency the node yields. Reading
    the base's ireq prints the base's spelling; reading the extras
    candidate's prints nothing, because it has none.
    """
    node = "requires-simple-extra[extra]"
    ireqs = [install_req_from_line(node)]
    inputs = collect_inputs(ireqs, ignore_dependencies=False)
    index = PipHostIndex(
        factory=factory,
        finder=yanking_finder,
        inputs=inputs,
        upgrade_strategy="to-satisfy-only",
        make_install_req=install_req_from_line,
    )
    provider = _provider(index, inputs)

    name = canonicalize_name("requires-simple-extra")
    version = index.candidates(name)[-1].version
    assert provider.get_dependencies(node, version)

    simple = canonicalize_name("simple")
    chosen = index.find(simple, Version("1.0"))
    assert chosen is not None
    dependency = index.pip_candidate(chosen, frozenset()).get_install_requirement()
    assert dependency is not None
    parent = dependency.comes_from
    assert isinstance(parent, InstallRequirement)
    assert parent.from_path() == node


def test_a_transitively_reached_extras_node_records_who_asked(
    factory: Factory, yanking_finder: PackageFinder
) -> None:
    index = _index(factory, yanking_finder, ["simple"])
    parent = install_req_from_line("parent==1.0")
    index.note_node_requirement("pkg[ext]", "pkg[ext]>=1", parent)

    recorded = index.node_requirement("pkg[ext]")
    assert recorded is not None
    assert recorded.from_path() == "pkg[ext]>=1->parent==1.0"
