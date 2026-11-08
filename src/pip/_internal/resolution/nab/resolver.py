"""Resolve native candidates with nab and prepare their installation order."""

from __future__ import annotations

import contextlib
import functools
import gc
import logging
import os
import sys
from collections import defaultdict
from collections.abc import Iterator
from typing import NamedTuple

from pip._vendor.nab_provider.preference import IncompletePreferenceError
from pip._vendor.nab_resolver.errors import (
    ResolutionError,
)
from pip._vendor.nab_resolver.resolver import DEFAULT_MAX_ITERATIONS
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import Version

from pip._internal.cache import WheelCache
from pip._internal.exceptions import InstallationError
from pip._internal.index.package_finder import PackageFinder
from pip._internal.operations.prepare import RequirementPreparer
from pip._internal.req.constructors import install_req_extend_extras
from pip._internal.req.req_install import InstallRequirement
from pip._internal.req.req_set import RequirementSet
from pip._internal.resolution.base import BaseResolver, InstallRequirementProvider
from pip._internal.resolution.nab.base import (
    Candidate,
    Requirement,
    SourceDomainChanged,
)
from pip._internal.resolution.nab.catalogue import PipCatalogue
from pip._internal.resolution.nab.factory import CollectedRootRequirements, Factory
from pip._internal.resolution.nab.reporter import (
    PipDebuggingReporter,
    PipProgressReporter,
)
from pip._internal.utils.packaging import get_requirement

logger = logging.getLogger(__name__)
_IS_CPYTHON = sys.implementation.name == "cpython"

DependencyGraph = dict[str | None, set[str | None]]


@contextlib.contextmanager
def _collector_paused() -> Iterator[None]:
    """Restore a collector that this context temporarily disabled."""
    # CPython still releases acyclic objects through reference counting.
    enabled = _IS_CPYTHON and gc.isenabled()
    if enabled:
        gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


class Result(NamedTuple):
    """Selected native candidates and edges used to order their installation."""

    mapping: dict[str, Candidate]
    graph: DependencyGraph
    requirements: dict[str, list[Requirement]] | None = None
    index_reinstalls: frozenset[str] = frozenset()


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

        self._make_factory = functools.partial(
            Factory,
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
        self.factory = self._make_factory()
        self.ignore_dependencies = ignore_dependencies
        self.only_dependencies = only_dependencies
        self.upgrade_strategy = upgrade_strategy
        self._result: Result | None = None

    def _resolve(self, collected: CollectedRootRequirements) -> Result:
        factory = self.factory
        reporter = (
            PipDebuggingReporter() if "PIP_RESOLVER_DEBUG" in os.environ else None
        )
        progress = PipProgressReporter()
        remaining = DEFAULT_MAX_ITERATIONS
        try_live = True
        with _collector_paused(), factory.catalogue_context():
            while True:
                epoch = factory.source_epoch
                catalogue = PipCatalogue(
                    factory,
                    collected,
                    ignore_dependencies=self.ignore_dependencies,
                    upgrade_strategy=self.upgrade_strategy,
                )
                try:
                    solution = catalogue.solve(
                        reporter,
                        max_rounds=remaining,
                        on_candidate_considered=progress.candidate_considered,
                        try_live=try_live,
                    )
                except SourceDomainChanged as error:
                    if factory.source_epoch == epoch:
                        raise InstallationError(str(error)) from error
                    remaining = catalogue.remaining_rounds
                    # Newly observed source choices need the full admission path.
                    try_live = False
                    logger.info("Nab source-domain restart: %s", error)
                    continue
                except IncompletePreferenceError as error:
                    raise InstallationError(
                        f"No matching installation found: {error}"
                    ) from error
                except ResolutionError as error:
                    raise InstallationError(str(error)) from error
                break

        logger.info("Nab catalogue success")
        graph: DependencyGraph = {None: set(solution.roots)}
        graph.update((package, set()) for package in solution.pins)
        for parent, child in solution.edges:
            graph[parent].add(child)

        requirements: dict[str, list[Requirement]] = defaultdict(list)
        for declarations in catalogue.selected_requirements(solution).values():
            for requirement in declarations:
                requirements[requirement.project_name].append(requirement)
        return Result(
            dict(catalogue.selected(solution)),
            graph,
            requirements,
            catalogue.selected_index_reinstalls(),
        )

    def installed_version_satisfies(
        self, package: str, version: Version, collected: CollectedRootRequirements
    ) -> bool:
        """Check original version bounds before skipping a same-version installation."""
        assert self._result is not None and self._result.requirements is not None
        constraint = collected.constraints.get(package)
        if constraint is not None and not constraint.specifier.contains(
            version, prereleases=True
        ):
            return False
        for requirement in self._result.requirements.get(package, ()):
            _, ireq = requirement.get_candidate_lookup()
            if ireq is not None and not ireq.specifier.contains(
                version, prereleases=True
            ):
                return False
        return True

    def resolve(
        self, root_reqs: list[InstallRequirement], check_supported_wheels: bool
    ) -> RequirementSet:
        """Prepare installation actions and merge the selected extras."""
        collected = self.factory.collect_root_requirements(root_reqs)
        result = self._result = self._resolve(collected)

        req_set = RequirementSet(check_supported_wheels=check_supported_wheels)
        # Base requirements must exist before their extras can be merged.
        for candidate in sorted(
            result.mapping.values(), key=lambda c: c.name != c.project_name
        ):
            ireq = candidate.get_install_requirement()
            if ireq is None:
                if candidate.name != candidate.project_name:
                    with contextlib.suppress(KeyError):
                        req = req_set.get_requirement(candidate.project_name)
                        req_set.add_named_requirement(
                            install_req_extend_extras(
                                req, get_requirement(candidate.name).extras
                            )
                        )
                continue

            installed_dist = self.factory.get_dist_to_uninstall(candidate)
            if installed_dist is None:
                ireq.should_reinstall = False
            elif self.factory.force_reinstall:
                ireq.should_reinstall = True
            elif candidate.project_name in result.index_reinstalls:
                ireq.should_reinstall = True
            elif installed_dist.version != candidate.version or (
                str(installed_dist.version) != str(candidate.version)
                and not self.installed_version_satisfies(
                    candidate.project_name, installed_dist.version, collected
                )
            ):
                ireq.should_reinstall = True
            elif candidate.is_editable or installed_dist.editable:
                # A matching version does not establish unchanged editable source.
                ireq.should_reinstall = True
            elif candidate.source_link and candidate.source_link.is_file:
                if candidate.source_link.is_wheel:
                    # Use --force-reinstall to replace a same-version local wheel.
                    logger.info(
                        "%s is already installed with the same version as the "
                        "provided wheel. Use --force-reinstall to force an "
                        "installation of the wheel.",
                        ireq.name,
                    )
                    continue

                ireq.should_reinstall = True
            else:
                continue

            link = candidate.source_link
            if link and link.is_yanked:
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
        """Order dependencies first, with arbitrary ordering when cycles remain.

        Weight calculation prunes leaves from the stored result graph in place.
        """
        assert self._result is not None, "must call resolve() first"

        if not req_set.requirements:
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
    """Weight installation keys by leaf layer, then recorded path depths.

    Prune leaves in place; each layer uses the graph's current non-root node count.

    The remaining graph uses the largest recorded depth from the None root.
    Each path excludes repeated nodes.

    Stop traversing installation keys after five recorded depths.
    Other nodes have no recorded-depth limit.
    """
    path: set[str | None] = set()
    weights: dict[str | None, list[int]] = {}

    def visit(node: str | None) -> None:
        if node in path:
            return

        # Only installation keys accumulate depths in weights.
        cur_weights = weights.get(node, [])
        if len(cur_weights) >= 5:
            return

        path.add(node)
        for child in graph[node]:
            visit(child)
        path.remove(node)

        if node not in requirement_keys:
            return

        cur_weights.append(len(path))
        weights[node] = cur_weights

    while True:
        leaves = set()
        for key in graph:
            if key is None:
                continue
            for _child in graph[key]:
                break
            else:
                leaves.add(key)

        if not leaves:
            break

        weight = len(graph) - 1
        for leaf in leaves:
            if leaf not in requirement_keys:
                continue
            weights[leaf] = [weight]

        for leaf in leaves:
            del graph[leaf]
        for children in graph.values():
            children.difference_update(leaves)

    # None is the virtual root joining all requested packages.
    visit(None)

    # Only installation keys may have weights.
    difference = set(weights.keys()).difference(requirement_keys)
    assert not difference, difference

    return {node: max(wgts) for (node, wgts) in weights.items()}


def _req_set_item_sorter(
    item: tuple[str, InstallRequirement],
    weights: dict[str | None, int],
) -> tuple[int, str]:
    """Break equal installation weights by canonical package name."""
    name = canonicalize_name(item[0])
    return weights[name], name
