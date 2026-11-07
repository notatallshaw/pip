"""Supply Pip's native requirement and candidate meanings to Nab."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence

from pip._vendor.nab_provider.callbacks import CandidateQuery

from .base import Candidate, Constraint, Requirement
from .factory import CollectedRootRequirements, Factory
from .reporter import PipProgressReporter


class PipHost:
    def __init__(
        self,
        factory: Factory,
        collected: CollectedRootRequirements,
        *,
        ignore_dependencies: bool,
        upgrade_strategy: str,
    ) -> None:
        self.progress = PipProgressReporter()
        self.factory = factory
        self.collected = collected
        self.ignore_dependencies = ignore_dependencies
        self.upgrade_strategy = upgrade_strategy

    @staticmethod
    def identify(requirement_or_candidate: Requirement | Candidate) -> str:
        return requirement_or_candidate.name

    @staticmethod
    def is_satisfied_by(requirement: Requirement, candidate: Candidate) -> bool:
        return requirement.is_satisfied_by(candidate)

    def find_matches(
        self,
        package: str,
        requirements: Mapping[str, Sequence[Requirement]],
        incompatibilities: Mapping[str, Sequence[Candidate]],
    ) -> CandidateQuery[Candidate]:
        base = package.partition("[")[0]
        eligible_for_upgrade = self.upgrade_strategy == "eager" or (
            self.upgrade_strategy == "only-if-needed"
            and base in self.collected.user_requested
        )
        query = self.factory.find_candidates(
            identifier=package,
            requirements=requirements,
            incompatibilities={
                name: iter(candidates) for name, candidates in incompatibilities.items()
            },
            constraint=self.factory.input_options.get(base, Constraint.empty()),
            prefers_installed=not eligible_for_upgrade,
            is_satisfied_by=self.is_satisfied_by,
            resolve_installed_source=(
                None
                if self.ignore_dependencies
                else self.factory.installed_self_candidate
            ),
        )
        query.candidates = self._observe_candidates(query.candidates)
        return query

    def _observe_candidates(
        self, candidates: Iterable[Candidate]
    ) -> Iterable[Candidate]:
        """Report candidate visits from the native stream."""
        for candidate in candidates:
            self.progress.candidate_considered(candidate)
            yield candidate

    def exact_candidate(self, requirement: Requirement) -> Candidate | None:
        candidate, _ = requirement.get_candidate_lookup()
        if candidate is None or requirement.is_python:
            return None
        if not self.factory.input_options.get(
            candidate.project_name, Constraint.empty()
        ).is_satisfied_by(candidate):
            return None
        return candidate

    def get_dependencies(self, candidate: Candidate) -> Iterable[Requirement]:
        for requirement in candidate.iter_dependencies(not self.ignore_dependencies):
            if requirement is None:
                continue
            yield requirement
            # An unsupported interpreter must fail before preparing child URLs.
            if requirement.is_python and requirement.get_candidate_lookup()[0] is None:
                return

    def preference(
        self, package: str, requirements: Mapping[str, Sequence[Requirement]]
    ) -> tuple[bool, bool, bool, bool, bool, float, bool]:
        declarations = requirements.get(package, ())
        operators = [
            (specifier.operator, specifier.version)
            for requirement in declarations
            if (ireq := requirement.get_candidate_lookup()[1]) is not None
            for specifier in ireq.specifier
        ]
        return (
            package != "<Python from Requires-Python>",
            not any(
                requirement.candidate is not None and not requirement.is_python
                for requirement in declarations
            ),
            not any(
                operator[:2] == "==" and "*" not in version
                for operator, version in operators
            ),
            not self.factory.has_installed(package),
            package != package.partition("[")[0],
            self.collected.user_requested.get(package.partition("[")[0], math.inf),
            not bool(operators),
        )
