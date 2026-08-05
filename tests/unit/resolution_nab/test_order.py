"""Installation order over a graph the nab adapter builds.

``tests/unit/resolution_resolvelib/test_resolver.py`` pins the same
expectations, but it builds resolvelib's own ``DirectedGraph`` and stubs a
resolvelib ``Result``, so it cannot say anything about the graph a
PubGrub-derived edge list produces. These cases run the identical
expectations through ``MutableGraph`` and through the nab resolver's
``get_installation_order``.
"""

from __future__ import annotations

from unittest import mock

import pytest

from pip._internal.req.constructors import install_req_from_line
from pip._internal.req.req_set import RequirementSet
from pip._internal.resolution._order import MutableGraph, get_topological_weights
from pip._internal.resolution.nab.engine import Solution
from pip._internal.resolution.nab.resolver import Resolver


def _resolver() -> Resolver:
    return Resolver(
        preparer=mock.Mock(),
        finder=mock.Mock(),
        wheel_cache=None,
        make_install_req=mock.Mock(),
        use_user_site=False,
        ignore_dependencies=False,
        only_dependencies=False,
        ignore_installed=True,
        ignore_requires_python=False,
        force_reinstall=False,
        upgrade_strategy="to-satisfy-only",
    )


@pytest.mark.parametrize(
    "edges, ordered_reqs",
    [
        (
            [(None, "require-simple"), ("require-simple", "simple")],
            ["simple==3.0", "require-simple==1.0"],
        ),
        (
            [(None, "meta"), ("meta", "simple"), ("meta", "simple2")],
            ["simple2==3.0", "simple==3.0", "meta==1.0"],
        ),
        (
            [
                (None, "toporequires"),
                (None, "toporequires2"),
                (None, "toporequires3"),
                (None, "toporequires4"),
                ("toporequires2", "toporequires"),
                ("toporequires3", "toporequires"),
                ("toporequires4", "toporequires"),
                ("toporequires4", "toporequires2"),
                ("toporequires4", "toporequires3"),
            ],
            [
                "toporequires==0.0.1",
                "toporequires3==0.0.1",
                "toporequires2==0.0.1",
                "toporequires4==0.0.1",
            ],
        ),
    ],
)
def test_nab_get_installation_order(
    edges: list[tuple[str | None, str]], ordered_reqs: list[str]
) -> None:
    resolver = _resolver()
    resolver._solution = Solution(pins=(), edges=tuple(edges), roots=())

    reqset = RequirementSet()
    for line in ordered_reqs:
        reqset.add_named_requirement(install_req_from_line(line))

    ireqs = resolver.get_installation_order(reqset)
    assert [str(ireq.req) for ireq in ireqs] == ordered_reqs


def test_nab_get_installation_order_needs_resolve_first() -> None:
    with pytest.raises(AssertionError, match="must call resolve"):
        _resolver().get_installation_order(RequirementSet())


def test_nab_get_installation_order_empty_req_set() -> None:
    resolver = _resolver()
    resolver._solution = Solution(pins=(), edges=((None, "simple"),), roots=())
    assert resolver.get_installation_order(RequirementSet()) == []


@pytest.mark.parametrize(
    "name, edges, requirement_keys, expected_weights",
    [
        (
            "deep second edge",
            [
                (None, "one"),
                (None, "two"),
                ("one", "five"),
                ("two", "three"),
                ("three", "four"),
                ("four", "five"),
            ],
            {"one", "two", "three", "four", "five"},
            {"five": 5, "four": 4, "one": 4, "three": 2, "two": 1},
        ),
        (
            "linear",
            [
                (None, "one"),
                ("one", "two"),
                ("two", "three"),
                ("three", "four"),
                ("four", "five"),
            ],
            {"one", "two", "three", "four", "five"},
            {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5},
        ),
        (
            "linear AND restricted",
            [
                (None, "one"),
                ("one", "two"),
                ("two", "three"),
                ("three", "four"),
                ("four", "five"),
            ],
            {"one", "three", "five"},
            {"one": 1, "three": 3, "five": 5},
        ),
        (
            "linear AND root -> two",
            [
                (None, "one"),
                ("one", "two"),
                ("two", "three"),
                ("three", "four"),
                ("four", "five"),
                (None, "two"),
            ],
            {"one", "two", "three", "four", "five"},
            {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5},
        ),
        (
            "linear AND root -> five",
            [
                (None, "one"),
                ("one", "two"),
                ("two", "three"),
                ("three", "four"),
                ("four", "five"),
                (None, "five"),
            ],
            {"one", "two", "three", "four", "five"},
            {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5},
        ),
        (
            "linear AND one -> four",
            [
                (None, "one"),
                ("one", "two"),
                ("two", "three"),
                ("three", "four"),
                ("four", "five"),
                ("one", "four"),
            ],
            {"one", "two", "three", "four", "five"},
            {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5},
        ),
        (
            "linear AND four -> one (cycle)",
            [
                (None, "one"),
                ("one", "two"),
                ("two", "three"),
                ("three", "four"),
                ("four", "five"),
                ("four", "one"),
            ],
            {"one", "two", "three", "four", "five"},
            {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5},
        ),
        (
            "linear AND four -> three (cycle)",
            [
                (None, "one"),
                ("one", "two"),
                ("two", "three"),
                ("three", "four"),
                ("four", "five"),
                ("four", "three"),
            ],
            {"one", "two", "three", "four", "five"},
            {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5},
        ),
        (
            "linear AND four -> three (cycle) AND restricted 4-5",
            [
                (None, "one"),
                ("one", "two"),
                ("two", "three"),
                ("three", "four"),
                ("four", "five"),
                ("four", "three"),
            ],
            {"four", "five"},
            {"four": 4, "five": 5},
        ),
    ],
)
def test_nab_topological_weights(
    name: str,
    edges: list[tuple[str | None, str]],
    requirement_keys: set[str],
    expected_weights: dict[str | None, int],
) -> None:
    graph = MutableGraph.from_edges(edges)
    assert get_topological_weights(graph, requirement_keys) == expected_weights


def test_mutable_graph_canonicalizes_and_keeps_the_none_root() -> None:
    graph = MutableGraph.from_edges([(None, "Foo_Bar"), ("Foo.Bar", "baz")])
    assert None in graph
    assert "foo-bar" in graph
    assert list(graph.iter_children(None)) == ["foo-bar"]
    assert list(graph.iter_children("foo-bar")) == ["baz"]


def test_mutable_graph_remove_drops_incoming_edges() -> None:
    graph = MutableGraph.from_edges([(None, "one"), ("one", "two")])
    graph.remove("two")
    assert list(graph.iter_children("one")) == []
    assert len(graph) == 2
