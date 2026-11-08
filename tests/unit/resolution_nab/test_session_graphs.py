"""Resolve independent wheel graphs through pip's prepared session."""

from pathlib import Path

import pytest

from pip._internal.exceptions import InstallationError
from pip._internal.index.package_finder import PackageFinder
from pip._internal.models.search_scope import SearchScope
from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.nab.catalogue import PipCatalogue
from pip._internal.resolution.nab.factory import Factory

from tests.lib.wheel import make_wheel


@pytest.mark.parametrize(
    "scenario_name,graph,roots,constraints,expected,edges",
    [
        (
            "chain",
            {"first": {"1": ["second>=1"]}, "second": {"1": [], "2": []}},
            ["first"],
            [],
            {"first": "1", "second": "2"},
            {("first", "second")},
        ),
        (
            "backtrack",
            {"first": {"1": ["second==1"], "2": ["second==2"]}, "second": {"1": []}},
            ["first"],
            [],
            {"first": "1", "second": "1"},
            {("first", "second")},
        ),
        (
            "diamond",
            {
                "first": {"1": ["third==1"], "2": ["third==2"]},
                "second": {"1": ["third==1"], "2": ["third==1"]},
                "third": {"1": [], "2": []},
            },
            ["first", "second"],
            [],
            {"first": "1", "second": "2", "third": "1"},
            {("first", "third"), ("second", "third")},
        ),
        (
            "cycle",
            {"first": {"1": ["second==1"]}, "second": {"1": ["first==1"]}},
            ["first"],
            [],
            {"first": "1", "second": "1"},
            {("first", "second"), ("second", "first")},
        ),
        (
            "constraint",
            {"first": {"1": ["second>=1"]}, "second": {"1": [], "2": []}},
            ["first"],
            ["second==1"],
            {"first": "1", "second": "1"},
            {("first", "second")},
        ),
        (
            "conflict",
            {"first": {"1": ["second==2"]}, "second": {"1": []}},
            ["first"],
            [],
            None,
            set(),
        ),
    ],
    ids=["chain", "backtrack", "diamond", "cycle", "constraint", "conflict"],
)
def test_local_wheel_session_contracts(
    factory: Factory,
    finder: PackageFinder,
    tmp_path: Path,
    scenario_name: str,
    graph: dict[str, dict[str, list[str]]],
    roots: list[str],
    constraints: list[str],
    expected: dict[str, str] | None,
    edges: set[tuple[str, str]],
) -> None:
    # Pip caches directory listings across resolver instances.
    directory = tmp_path / scenario_name
    directory.mkdir()
    artifacts: dict[tuple[str, str], Path] = {}
    for package, versions in graph.items():
        for version, dependencies in versions.items():
            wheel = make_wheel(
                package, version, metadata_updates={"Requires-Dist": dependencies}
            )
            artifacts[package, version] = Path(wheel.save_to_dir(directory))

    finder.search_scope = SearchScope([str(directory)], [], False)
    inputs = [install_req_from_line(item) for item in roots]
    inputs.extend(install_req_from_line(item, constraint=True) for item in constraints)
    collected = factory.collect_root_requirements(inputs)
    catalogue = PipCatalogue(
        factory,
        collected,
        ignore_dependencies=False,
        upgrade_strategy="to-satisfy-only",
    )

    if expected is None:
        with pytest.raises(InstallationError):
            catalogue.solve()
        return

    # Repeated solves must preserve the selected files and dependency graph.
    for _ in range(2):
        solution = catalogue.solve()
        selected = catalogue.selected(solution)
        assert {
            name: str(candidate.version) for name, candidate in selected.items()
        } == expected
        assert set(solution.edges) == edges
        for package, candidate in selected.items():
            assert candidate.source_link is not None
            assert (
                candidate.source_link.url
                == artifacts[package, expected[package]].as_uri()
            )
