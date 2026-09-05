"""Supply pip's native prepared candidates and requirements to nab."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from pip._vendor.nab_resolver.candidate_provider import (
    CandidateRequirement,
    PreparedCandidate,
)
from pip._vendor.nab_resolver.types import RangeProtocol
from pip._vendor.resolvelib.resolvers import RequirementInformation

from pip._internal.models.link import Link, links_equivalent
from pip._internal.resolution.nab.ranges import CandidateKey, CandidateRange
from pip._internal.resolution.resolvelib.base import Candidate, Requirement
from pip._internal.resolution.resolvelib.factory import (
    CollectedRootRequirements,
    Factory,
)
from pip._internal.resolution.resolvelib.provider import PipProvider


@dataclass(frozen=True)
class Request:
    """Retain the native requirement and the candidate that requested it."""

    requirement: Requirement
    parent: Candidate | None

    def __str__(self) -> str:
        return str(self.requirement)


@dataclass(frozen=True)
class SelfRefinement:
    """Retain installed metadata obligations when selecting a replacement."""

    previous: Candidate
    candidate: Candidate
    dependencies: tuple[Requirement, ...]


def native_candidate(prepared: PreparedCandidate[CandidateKey]) -> Candidate:
    """Return the candidate whose distribution will be installed."""
    origin = prepared.origin
    return (
        origin.candidate
        if isinstance(origin, SelfRefinement)
        else cast(Candidate, origin)
    )


class NativeHost:
    """Keep pip's existing finder, preparation and dependency policies."""

    def __init__(self, factory: Factory, provider: PipProvider) -> None:
        self.factory = factory
        self.provider = provider
        self.sources: list[tuple[Link, bool]] = []
        self.dependencies: dict[Candidate, tuple[Requirement, ...]] = {}
        self.refinements: dict[tuple[str, CandidateKey], SelfRefinement] = {}

    def availability_generation(self) -> int:
        return len(self.sources) + len(self.refinements)

    def _source(self, candidate: Candidate) -> str:
        link = candidate.source_link
        if link is None:
            return (
                "installed" if candidate.is_installed else "internal:" + candidate.name
            )
        return self._link_source(link, candidate.is_editable)

    def _link_source(self, link: Link, editable: bool) -> str:
        for number, (known, was_editable) in enumerate(self.sources):
            if was_editable == editable and links_equivalent(known, link):
                return f"link:{number}"
        self.sources.append((link, editable))
        return f"link:{len(self.sources) - 1}"

    def bind(
        self,
        requirement: Requirement,
        parent: Candidate | None = None,
    ) -> CandidateRequirement[str, CandidateKey]:
        candidate, ireq = requirement.get_candidate_lookup()
        if candidate is not None:
            constraint = CandidateRange.singleton(
                CandidateKey(candidate.version, self._source(candidate))
            )
            if candidate.source_link is not None:
                constraint |= CandidateRange.singleton(self._refinement_key(candidate))
        elif ireq is not None:
            constraint = CandidateRange(ireq.specifier.to_range())
        else:
            constraint = CandidateRange.empty()
        return CandidateRequirement(
            requirement.name, constraint, Request(requirement, parent)
        )

    def constraints(
        self, collected: CollectedRootRequirements
    ) -> dict[str, CandidateRange]:
        result = {}
        for package, constraint in collected.constraints.items():
            bounds = CandidateRange(constraint.specifier.to_range())
            for link in constraint.links:
                ordinary = CandidateRange.for_source(self._link_source(link, False))
                editable = CandidateRange.for_source(self._link_source(link, True))
                bounds &= ordinary | editable
            result[package] = bounds
        for requirement in collected.requirements:
            if requirement.project_name in result:
                result[requirement.name] = result[requirement.project_name]
        return result

    def iter_candidates(
        self,
        package: str,
        allowed: RangeProtocol[CandidateKey],
        requirements: Mapping[str, Sequence[CandidateRequirement[str, CandidateKey]]],
    ) -> Iterable[PreparedCandidate[CandidateKey]]:
        native = {
            name: tuple(cast(Request, cause.origin).requirement for cause in causes)
            for name, causes in requirements.items()
        }
        if package not in native:
            return
        for candidate in self.provider.find_matches(package, native, {}):
            for prepared in self._prepared_candidates(candidate, native):
                if prepared.key in allowed:
                    yield prepared

    def get_dependencies(
        self, candidate: PreparedCandidate[CandidateKey]
    ) -> Iterable[CandidateRequirement[str, CandidateKey]]:
        origin = candidate.origin
        if isinstance(origin, SelfRefinement):
            for requirement in origin.dependencies:
                yield self.bind(requirement, origin.previous)
        native = native_candidate(candidate)
        for requirement in self._dependencies_for(native):
            yield self.bind(requirement, native)

    def installation_graph(
        self, roots: Iterable[str], selected: Mapping[str, Candidate]
    ) -> tuple[dict[str, Candidate], tuple[tuple[str, str], ...]]:
        """Keep only dependencies of the distributions actually selected."""
        mapping: dict[str, Candidate] = {}
        edges = []
        pending = deque(roots)
        while pending:
            package = pending.popleft()
            if package in mapping:
                continue
            candidate = selected[package]
            mapping[package] = candidate
            for requirement in self.provider.get_dependencies(candidate):
                edges.append((package, requirement.name))
                pending.append(requirement.name)
        return mapping, tuple(edges)

    def _refinement_key(self, candidate: Candidate) -> CandidateKey:
        """Separate replacement metadata from installed metadata obligations."""
        return CandidateKey(candidate.version, "refinement:" + self._source(candidate))

    def _dependencies_for(self, candidate: Candidate) -> tuple[Requirement, ...]:
        """Cache native dependencies up to an intrinsically empty intersection."""
        dependencies = self.dependencies.get(candidate)
        if dependencies is None:
            pending = []
            constraints: dict[str, CandidateRange] = {}
            for requirement in self.provider.get_dependencies(candidate):
                pending.append(requirement)
                cause = self.bind(requirement, candidate)
                constraint = (
                    constraints.get(cause.package, CandidateRange.full())
                    & cause.constraint
                )
                constraints[cause.package] = constraint
                if constraint.is_empty:
                    break
            dependencies = tuple(pending)
            self.dependencies[candidate] = dependencies
        return dependencies

    def _prepared_candidates(
        self, candidate: Candidate, requirements: Mapping[str, Sequence[Requirement]]
    ) -> Iterable[PreparedCandidate[CandidateKey]]:
        """Prepare an installed self-replacement as a distinct decision."""
        if not candidate.is_installed:
            yield PreparedCandidate(
                CandidateKey(candidate.version, self._source(candidate)), candidate
            )
            return
        dependencies = self._dependencies_for(candidate)
        self_requirements = tuple(
            requirement
            for requirement in dependencies
            if requirement.name == candidate.name
        )
        if all(
            requirement.is_satisfied_by(candidate) for requirement in self_requirements
        ):
            yield PreparedCandidate(
                CandidateKey(candidate.version, self._source(candidate)), candidate
            )
            return
        prospective = dict(requirements)
        prospective[candidate.name] = (
            *requirements[candidate.name],
            *self_requirements,
        )
        for replacement in self.provider.find_matches(candidate.name, prospective, {}):
            if not all(
                requirement.is_satisfied_by(replacement)
                for requirement in self_requirements
            ):
                continue
            key = self._refinement_key(replacement)
            origin = self.refinements.setdefault(
                (candidate.name, key),
                SelfRefinement(candidate, replacement, dependencies),
            )
            yield PreparedCandidate(key, origin)

    def priority(
        self,
        package: str,
        requirements: Mapping[str, Sequence[CandidateRequirement[str, CandidateKey]]],
    ) -> Any:
        information = {
            package: tuple(
                RequirementInformation(
                    cast(Request, cause.origin).requirement,
                    cast(Request, cause.origin).parent,
                )
                for cause in requirements.get(package, ())
            )
        }
        return self.provider.get_preference(package, {}, {}, information, ())
