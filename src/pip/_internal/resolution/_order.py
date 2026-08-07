"""Installation ordering.

``get_topological_weights`` and ``_req_set_item_sorter`` are pinned by 17
parametrised unit cases. They take a graph through a structural protocol so
the ordering rule is independent of how a resolver records its edges.

``MutableGraph`` is the graph implementation a resolver builds to feed them.
``get_topological_weights`` prunes leaves by calling ``graph.remove()``, so
the graph has to be mutable and it has to carry the ``None`` root.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Protocol

from pip._vendor.packaging.utils import canonicalize_name

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from pip._internal.req.req_install import InstallRequirement
    from pip._internal.req.req_set import RequirementSet


class WeightGraph(Protocol):
    """The part of a dependency graph ``get_topological_weights`` reads.

    :class:`MutableGraph` satisfies this structurally.
    """

    def __iter__(self) -> Iterator[str | None]: ...

    def __len__(self) -> int: ...

    def iter_children(self, key: str | None) -> Iterable[str | None]: ...

    def remove(self, key: str | None) -> None: ...


class MutableGraph:
    """A directed graph over resolver keys, rooted at ``None``.

    Built from the edge list a resolver records during the solve. Only the
    operations ``get_topological_weights`` needs are implemented.
    """

    def __init__(self) -> None:
        self._vertices: set[str | None] = set()
        self._forwards: dict[str | None, set[str | None]] = {}

    @classmethod
    def from_edges(cls, edges: Iterable[tuple[str | None, str | None]]) -> MutableGraph:
        """Build a graph from ``(parent, child)`` pairs.

        A parent of ``None`` means the requirement was requested by the user.
        Names are canonicalized, because ``_req_set_item_sorter`` looks the
        weights up under the canonical name.
        """
        graph = cls()
        graph.add(None)
        for parent, child in edges:
            for vertex in (
                None if parent is None else canonicalize_name(parent),
                None if child is None else canonicalize_name(child),
            ):
                graph.add(vertex)
            graph.connect(
                None if parent is None else canonicalize_name(parent),
                None if child is None else canonicalize_name(child),
            )
        return graph

    def __iter__(self) -> Iterator[str | None]:
        return iter(list(self._vertices))

    def __len__(self) -> int:
        return len(self._vertices)

    def __contains__(self, key: str | None) -> bool:
        return key in self._vertices

    def add(self, key: str | None) -> None:
        if key in self._vertices:
            return
        self._vertices.add(key)
        self._forwards[key] = set()

    def connect(self, f: str | None, t: str | None) -> None:
        self._forwards[f].add(t)

    def remove(self, key: str | None) -> None:
        self._vertices.remove(key)
        del self._forwards[key]
        for children in self._forwards.values():
            children.discard(key)

    def iter_children(self, key: str | None) -> Iterable[str | None]:
        return iter(self._forwards.get(key, ()))


def installation_order(
    req_set: RequirementSet, graph: WeightGraph
) -> list[InstallRequirement]:
    """Order ``req_set`` so a requirement comes before anything needing it.

    ``graph`` is mutated: ``get_topological_weights`` prunes it.
    """
    if not req_set.requirements:
        # Nothing is left to install, so we do not need an order.
        return []

    weights = get_topological_weights(graph, set(req_set.requirements.keys()))

    sorted_items = sorted(
        req_set.requirements.items(),
        key=functools.partial(_req_set_item_sorter, weights=weights),
        reverse=True,
    )
    return [ireq for _, ireq in sorted_items]


def get_topological_weights(
    graph: WeightGraph, requirement_keys: set[str]
) -> dict[str | None, int]:
    """Assign weights to each node based on how "deep" they are.

    This implementation may change at any point in the future without prior
    notice.

    We first simplify the dependency graph by pruning any leaves and giving them
    the highest weight: a package without any dependencies should be installed
    first. This is done again and again in the same way, giving ever less weight
    to the newly found leaves. The loop stops when no leaves are left: all
    remaining packages have at least one dependency left in the graph.

    Then we continue with the remaining graph, by taking the length for the
    longest path to any node from root, ignoring any paths that contain a single
    node twice (i.e. cycles). This is done through a depth-first search through
    the graph, while keeping track of the path to the node.

    Cycles in the graph result would result in node being revisited while also
    being on its own path. In this case, take no action. This helps ensure we
    don't get stuck in a cycle.

    When assigning weight, the longer path (i.e. larger length) is preferred.

    We are only interested in the weights of packages that are in the
    requirement_keys.
    """
    path: set[str | None] = set()
    weights: dict[str | None, list[int]] = {}

    def visit(node: str | None) -> None:
        if node in path:
            # We hit a cycle, so we'll break it here.
            return

        # The walk is exponential and for pathologically connected graphs (which
        # are the ones most likely to contain cycles in the first place) it can
        # take until the heat-death of the universe. To counter this we limit
        # the number of attempts to visit (i.e. traverse through) any given
        # node. We choose a value here which gives decent enough coverage for
        # fairly well behaved graphs, and still limits the walk complexity to be
        # linear in nature.
        cur_weights = weights.get(node, [])
        if len(cur_weights) >= 5:
            return

        # Time to visit the children!
        path.add(node)
        for child in graph.iter_children(node):
            visit(child)
        path.remove(node)

        if node not in requirement_keys:
            return

        cur_weights.append(len(path))
        weights[node] = cur_weights

    # Simplify the graph, pruning leaves that have no dependencies. This is
    # needed for large graphs (say over 200 packages) because the `visit`
    # function is slower for large/densely connected graphs, taking minutes.
    # See https://github.com/pypa/pip/issues/10557
    # We repeat the pruning step until we have no more leaves to remove.
    while True:
        leaves = set()
        for key in graph:
            if key is None:
                continue
            for _child in graph.iter_children(key):
                # This means we have at least one child
                break
            else:
                # No child.
                leaves.add(key)
        if not leaves:
            # We are done simplifying.
            break
        # Calculate the weight for the leaves.
        weight = len(graph) - 1
        for leaf in leaves:
            if leaf not in requirement_keys:
                continue
            weights[leaf] = [weight]
        # Remove the leaves from the graph, making it simpler.
        for leaf in leaves:
            graph.remove(leaf)

    # Visit the remaining graph, this will only have nodes to handle if the
    # graph had a cycle in it, which the pruning step above could not handle.
    # `None` is the root node the resolver records every root under.
    visit(None)

    # Sanity check: all requirement keys should be in the weights,
    # and no other keys should be in the weights.
    difference = set(weights.keys()).difference(requirement_keys)
    assert not difference, difference

    # Now give back all the weights, choosing the largest ones from what we
    # accumulated.
    return {node: max(wgts) for (node, wgts) in weights.items()}


def _req_set_item_sorter(
    item: tuple[str, InstallRequirement],
    weights: dict[str | None, int],
) -> tuple[int, str]:
    """Key function used to sort install requirements for installation.

    Based on the "weight" mapping calculated in ``get_installation_order()``.
    The canonical package name is returned as the second member as a tie-
    breaker to ensure the result is predictable, which is useful in tests.
    """
    name = canonicalize_name(item[0])
    return weights[name], name
