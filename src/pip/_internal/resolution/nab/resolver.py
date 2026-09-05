"""Run nab while retaining pip's native candidate and installation plumbing."""

from __future__ import annotations

import os
from typing import Any

from pip._vendor.nab_provider.candidate_ranges import CandidateKey, CandidateRange
from pip._vendor.nab_resolver.candidate_provider import CandidateProvider
from pip._vendor.nab_resolver.errors import ResolutionError
from pip._vendor.nab_resolver.resolver import Resolver as NabResolver
from pip._vendor.nab_resolver.resolver import ResolverObserver
from pip._vendor.packaging.version import Version
from pip._vendor.resolvelib.resolvers import Result
from pip._vendor.resolvelib.resolvers.criterion import Criterion
from pip._vendor.resolvelib.structs import DirectedGraph

from pip._internal.resolution.nab.errors import installation_error
from pip._internal.resolution.nab.host import (
    NativeHost,
    SelfRefinement,
    native_candidate,
)
from pip._internal.resolution.resolvelib.base import Candidate, Requirement
from pip._internal.resolution.resolvelib.factory import CollectedRootRequirements
from pip._internal.resolution.resolvelib.provider import PipProvider
from pip._internal.resolution.resolvelib.reporter import (
    PipDebuggingReporter,
    PipReporter,
)
from pip._internal.resolution.resolvelib.resolver import Resolver as InstallerResolver


class Resolver(InstallerResolver):
    def _resolve(
        self, collected: CollectedRootRequirements
    ) -> Result[Requirement, Candidate, str]:
        native = PipProvider(
            factory=self.factory,
            constraints=collected.constraints,
            ignore_dependencies=self.ignore_dependencies,
            only_dependencies=self.only_dependencies,
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
        graph: DirectedGraph[str | None] = DirectedGraph()
        graph.add(None)
        for package in mapping:
            graph.add(package)
        for package in solution.roots:
            graph.connect(None, package)
        for parent, child in edges:
            graph.connect(parent, child)
        return Result(mapping, graph, {})


class _Observer(ResolverObserver[str, CandidateKey]):
    def __init__(
        self, provider: CandidateProvider[str, CandidateKey], reporter: Any
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
            self.reporter.rejecting_candidate(Criterion([], (), ()), candidate)
