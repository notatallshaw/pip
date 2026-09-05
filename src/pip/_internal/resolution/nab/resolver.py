"""Resolve native candidates with nab and prepare their installation order."""

from __future__ import annotations

import contextlib
import functools
import logging
import os
from typing import Any, NamedTuple

from pip._vendor.nab_provider.candidate_ranges import CandidateKey, CandidateRange
from pip._vendor.nab_resolver.candidate_provider import CandidateProvider
from pip._vendor.nab_resolver.errors import ResolutionError
from pip._vendor.nab_resolver.resolver import Resolver as NabResolver
from pip._vendor.nab_resolver.resolver import ResolverObserver
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import Version

from pip._internal.cache import WheelCache
from pip._internal.index.package_finder import PackageFinder
from pip._internal.operations.prepare import RequirementPreparer
from pip._internal.req.constructors import install_req_extend_extras
from pip._internal.req.req_install import InstallRequirement
from pip._internal.req.req_set import RequirementSet
from pip._internal.resolution.base import BaseResolver, InstallRequirementProvider
from pip._internal.resolution.nab.base import Candidate
from pip._internal.resolution.nab.errors import installation_error
from pip._internal.resolution.nab.factory import CollectedRootRequirements, Factory
from pip._internal.resolution.nab.host import (
    NativeHost,
    SelfRefinement,
    native_candidate,
)
from pip._internal.resolution.nab.provider import PipProvider
from pip._internal.resolution.nab.reporter import (
    PipDebuggingReporter,
    PipReporter,
)
from pip._internal.utils.packaging import get_requirement

logger = logging.getLogger(__name__)

DependencyGraph = dict[str | None, set[str | None]]


class Result(NamedTuple):
    """Selected native candidates and edges used to order their installation."""

    mapping: dict[str, Candidate]
    graph: DependencyGraph


class Resolver(BaseResolver):
    """Prepare native requirements, solve them with nab, and order installations."""

    _allowed_strategies = {"eager", "only-if-needed", "to-satisfy-only"}

    def __init__(
        self,
        preparer: RequirementPreparer,
        finder: PackageFinder,
        wheel_cache: WheelCache | None,
        make_install_req: InstallRequirementProvider,
        use_user_site: bool,
        ignore_dependencies: bool,
        only_dependencies: bool,
        ignore_installed: bool,
        ignore_requires_python: bool,
        force_reinstall: bool,
        upgrade_strategy: str,
        py_version_info: tuple[int, ...] | None = None,
    ):
        super().__init__()
        assert upgrade_strategy in self._allowed_strategies
        assert not (ignore_dependencies and only_dependencies)

        self.factory = Factory(
            finder=finder,
            preparer=preparer,
            make_install_req=make_install_req,
            wheel_cache=wheel_cache,
            use_user_site=use_user_site,
            force_reinstall=force_reinstall,
            ignore_installed=ignore_installed,
            ignore_requires_python=ignore_requires_python,
            py_version_info=py_version_info,
        )
        self.ignore_dependencies = ignore_dependencies
        self.only_dependencies = only_dependencies
        self.upgrade_strategy = upgrade_strategy
        self._result: Result | None = None

    def _resolve(self, collected: CollectedRootRequirements) -> Result:
        """Select distributions, then discard obligations used only for validation."""
        native = PipProvider(
            factory=self.factory,
            constraints=collected.constraints,
            ignore_dependencies=self.ignore_dependencies,
            upgrade_strategy=self.upgrade_strategy,
            user_requested=collected.user_requested,
        )
        reporter = (
            PipDebuggingReporter()
            if "PIP_RESOLVER_DEBUG" in os.environ
            else PipReporter(collected.constraints)
        )
        reporter.starting()

        host = NativeHost(self.factory, native)
        provider = CandidateProvider(
            host, [host.bind(req) for req in collected.requirements]
        )
        resolver = NabResolver(
            provider,
            range_type=CandidateRange,
            root_version=CandidateKey(Version("0"), "root"),
            observer=_Observer(provider, reporter),
            availability_generation=host.availability_generation,
        )
        try:
            solution = resolver.solve(
                provider.root_requirements(), host.constraints(collected)
            )
        except ResolutionError as error:
            raise installation_error(
                error, provider, self.factory, collected.constraints
            ) from error

        selected = {
            package: provider.prepared(package, key)
            for package, key in solution.pins.items()
        }
        mapping = {
            package: native_candidate(candidate)
            for package, candidate in selected.items()
        }
        edges = solution.edges
        if any(
            isinstance(candidate.origin, SelfRefinement)
            for candidate in selected.values()
        ):
            mapping, edges = host.installation_graph(solution.roots, mapping)

        graph: DependencyGraph = {None: set(solution.roots)}
        graph.update((package, set()) for package in mapping)
        for parent, child in edges:
            graph[parent].add(child)
        return Result(mapping, graph)

    def resolve(
        self, root_reqs: list[InstallRequirement], check_supported_wheels: bool
    ) -> RequirementSet:
        """Prepare installation actions and merge the selected extras."""
        collected = self.factory.collect_root_requirements(root_reqs)
        result = self._result = self._resolve(collected)

        req_set = RequirementSet(check_supported_wheels=check_supported_wheels)
        # process candidates with extras last to ensure their base equivalent is
        # already in the req_set if appropriate.
        # Python's sort is stable so using a binary key function keeps relative order
        # within both subsets.
        for candidate in sorted(
            result.mapping.values(), key=lambda c: c.name != c.project_name
        ):
            ireq = candidate.get_install_requirement()
            if ireq is None:
                if candidate.name != candidate.project_name:
                    # extend existing req's extras
                    with contextlib.suppress(KeyError):
                        req = req_set.get_requirement(candidate.project_name)
                        req_set.add_named_requirement(
                            install_req_extend_extras(
                                req, get_requirement(candidate.name).extras
                            )
                        )
                continue

            # Check if there is already an installation under the same name,
            # and set a flag for later stages to uninstall it, if needed.
            installed_dist = self.factory.get_dist_to_uninstall(candidate)
            if installed_dist is None:
                # There is no existing installation -- nothing to uninstall.
                ireq.should_reinstall = False
            elif self.factory.force_reinstall:
                # The --force-reinstall flag is set -- reinstall.
                ireq.should_reinstall = True
            elif installed_dist.version != candidate.version:
                # The installation is different in version -- reinstall.
                ireq.should_reinstall = True
            elif candidate.is_editable or installed_dist.editable:
                # The incoming distribution is editable, or different in
                # editable-ness to installation -- reinstall.
                ireq.should_reinstall = True
            elif candidate.source_link and candidate.source_link.is_file:
                # The incoming distribution is under file://
                if candidate.source_link.is_wheel:
                    # is a local wheel -- do nothing.
                    logger.info(
                        "%s is already installed with the same version as the "
                        "provided wheel. Use --force-reinstall to force an "
                        "installation of the wheel.",
                        ireq.name,
                    )
                    continue

                # is a local sdist or path -- reinstall
                ireq.should_reinstall = True
            else:
                continue

            link = candidate.source_link
            if link and link.is_yanked:
                # The reason can contain non-ASCII characters, Unicode
                # is required for Python 2.
                msg = (
                    "The candidate selected for download or install is a "
                    "yanked version: {name!r} candidate (version {version} "
                    "at {link})\nReason for being yanked: {reason}"
                ).format(
                    name=candidate.name,
                    version=candidate.version,
                    link=link,
                    reason=link.yanked_reason or "<none given>",
                )
                logger.warning(msg)

            req_set.add_named_requirement(ireq)

        if self.only_dependencies:
            for requested in collected.user_requested:
                project_name = requested.partition("[")[0]
                req_set.requirements.pop(project_name, None)

        return req_set

    def get_installation_order(
        self, req_set: RequirementSet
    ) -> list[InstallRequirement]:
        """Get order for installation of requirements in RequirementSet.

        The returned list contains a requirement before another that depends on
        it. This helps ensure that the environment is kept consistent as they
        get installed one-by-one.

        The current implementation creates a topological ordering of the
        dependency graph, giving more weight to packages with less
        or no dependencies, while breaking any cycles in the graph at
        arbitrary points. We make no guarantees about where the cycle
        would be broken, other than it *would* be broken.
        """
        assert self._result is not None, "must call resolve() first"

        if not req_set.requirements:
            # Nothing is left to install, so we do not need an order.
            return []

        graph = self._result.graph
        weights = get_topological_weights(graph, set(req_set.requirements.keys()))

        sorted_items = sorted(
            req_set.requirements.items(),
            key=functools.partial(_req_set_item_sorter, weights=weights),
            reverse=True,
        )
        return [ireq for _, ireq in sorted_items]


def get_topological_weights(
    graph: DependencyGraph, requirement_keys: set[str]
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
        for child in graph[node]:
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
            for _child in graph[key]:
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
            del graph[leaf]
        for children in graph.values():
            children.difference_update(leaves)

    # Visit the remaining graph, this will only have nodes to handle if the
    # graph had a cycle in it, which the pruning step above could not handle.
    # None is the virtual root joining all requested packages.
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


class _Observer(ResolverObserver[str, CandidateKey]):
    """Report native distributions when nab accepts or rejects a decision."""

    def __init__(
        self,
        provider: CandidateProvider[str, CandidateKey],
        reporter: PipReporter | PipDebuggingReporter,
    ) -> None:
        self.provider = provider
        self.reporter = reporter
        self.decisions: dict[str, CandidateKey] = {}

    def on_decision(self, package: str, version: CandidateKey, level: int) -> None:
        self.decisions[package] = version
        self.reporter.pinning(
            native_candidate(self.provider.prepared(package, version))
        )

    def on_conflict_step(
        self,
        incompatibility: Any,
        *,
        satisfier_package: str,
        satisfier_is_decision: bool,
        satisfier_level: int,
        previous_level: int,
        can_backjump: bool,
    ) -> None:
        if satisfier_is_decision and satisfier_package in self.decisions:
            key = self.decisions[satisfier_package]
            candidate = native_candidate(self.provider.prepared(satisfier_package, key))
            # Conflict events identify the rejected decision without native causes.
            self.reporter.rejecting_candidate((), candidate)
