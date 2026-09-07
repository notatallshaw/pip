"""Pass original requirement provenance back to pip's error renderer."""

from __future__ import annotations

from collections import Counter
from typing import Any, cast

from pip._vendor.nab_resolver.candidate_provider import CandidateProvider
from pip._vendor.nab_resolver.errors import ResolutionError
from pip._vendor.nab_resolver.root import ROOT
from pip._vendor.nab_resolver.types import Incompatibility

from pip._internal.exceptions import InstallationError
from pip._internal.resolution.nab.base import Constraint, Requirement, RequirementCause
from pip._internal.resolution.nab.factory import Factory
from pip._internal.resolution.nab.host import NativeHost, Request
from pip._internal.resolution.nab.ranges import CandidateKey


def installation_error(
    error: ResolutionError,
    provider: CandidateProvider[str, CandidateKey],
    factory: Factory,
    constraints: dict[str, Constraint],
) -> Exception:
    """Render native conflict causes, or the solver error when no causes survive."""
    if error.incompatibility is None:
        return InstallationError(str(error))

    clauses = _external_clauses(error.incompatibility)
    selected = _select_requested_pairs(clauses)
    records = _native_causes(provider, clauses, selected)
    if not records:
        return InstallationError(str(error))
    return factory.get_installation_error(records, constraints)


def _select_requested_pairs(
    clauses: list[Incompatibility[Any, Any]],
) -> list[tuple[str | None, str, Any]]:
    """Choose representative requests from absence and contradictory dependencies."""
    blamed = _unavailable_packages(clauses)
    requested = [
        pair
        for clause in clauses
        if clause.cause.name in {"ROOT", "DEPENDENCY"}
        for pair in [_requested_pair(clause)]
        if pair is not None
    ]
    impossible = {
        pair
        for clause in clauses
        if (pair := _requested_pair(clause)) is not None
        and clause.cause.name == "DEPENDENCY"
        and any(
            not term.is_positive() and term.constraint.is_empty for term in clause.terms
        )
    }

    upstream = {parent for parent, child, _ in impossible if parent != child}
    selected = [
        pair
        for pair in requested
        if pair in impossible or (pair[1] in blamed and pair[1] not in upstream)
    ]

    if not selected:
        counts = Counter(package for _, package, _ in requested)
        selected = [
            pair for pair in requested if counts[pair[1]] == max(counts.values())
        ]
    return selected


def _native_causes(
    provider: CandidateProvider[str, CandidateKey],
    clauses: list[Incompatibility[Any, Any]],
    selected: list[tuple[str | None, str, Any]],
) -> list[RequirementCause]:
    """Recover native parents from the proof and deduplicate by object identity."""
    root_origins = {
        id(clause.origin) for clause in clauses if clause.cause.name == "ROOT"
    }
    records = []
    seen = set()
    for parent, package, parent_range in selected:
        if parent is None:
            causes = _root_causes(provider, package, root_origins)
        else:
            causes = [
                cause
                for recorded, candidate in provider.recorded_candidates()
                if recorded == parent and candidate.key in parent_range
                for cause in provider.causes_for(recorded, candidate.key)
                if cause.package == package
            ]
        for cause in causes:
            request = cast(Request, cause.origin)
            key = id(request.requirement), id(request.parent)
            if key not in seen:
                seen.add(key)
                records.append(RequirementCause(request.requirement, request.parent))
    return records


def _unavailable_packages(clauses: list[Incompatibility[Any, Any]]) -> set[str]:
    """Blame absence only when its ranges cover an original requested range."""
    unavailable: dict[str, Any] = {}
    blamed = set()
    for clause in clauses:
        package = _positive_package(clause)
        if package is None:
            continue
        if clause.cause.name == "CONSTRAINT":
            blamed.add(package)
        elif clause.cause.name in {"NO_VERSIONS", "CONTEXTUAL_NO_VERSIONS"}:
            bounds = clause.terms[0].constraint
            unavailable[package] = unavailable.get(package, bounds) | bounds

    # Guarded absence leaves guide diagnostics; they do not prove global absence.
    for clause in clauses:
        if clause.cause.name not in {"ROOT", "DEPENDENCY"}:
            continue
        pair = _requested_pair(clause)
        if pair is None or pair[1] not in unavailable:
            continue
        negative = [term for term in clause.terms if not term.is_positive()]
        requested = negative[0].constraint if negative else clause.dependency_range
        if requested is not None and requested.is_subset(unavailable[pair[1]]):
            blamed.add(pair[1])
    return blamed


def _root_causes(
    provider: CandidateProvider[str, CandidateKey], package: str, origins: set[int]
) -> list[Any]:
    """Stop at the first root request pip's native candidate search rejects."""
    host = cast(NativeHost, provider.host)
    causes = []
    requirements: dict[str, list[Requirement]] = {}
    for cause in provider.roots:
        if cause.package != package or id(cause.origin) not in origins:
            continue
        causes.append(cause)
        requirements.setdefault(package, []).append(
            cast(Request, cause.origin).requirement
        )
        if next(iter(host.provider.find_matches(package, requirements)), None) is None:
            break
    return causes


def _external_clauses(
    derivation: Incompatibility[Any, Any] | None,
) -> list[Incompatibility[Any, Any]]:
    """Walk shared proof nodes once and return the original external clauses."""
    stack = [derivation]
    seen = set()
    result = []
    while stack:
        clause = stack.pop()
        if clause is None or id(clause) in seen:
            continue
        seen.add(id(clause))
        if clause.cause.name == "DERIVED":
            stack.extend((clause.cause_left, clause.cause_right))
        else:
            result.append(clause)
    return result


def _positive_package(clause: Any) -> str | None:
    """Identify the unavailable package without confusing it with an absence guard."""
    if clause.cause.name == "CONTEXTUAL_NO_VERSIONS":
        package = clause.unavailable_package
        return package if isinstance(package, str) else None
    positive = [term for term in clause.terms if term.is_positive()]
    if len(positive) == 1 and isinstance(positive[0].package, str):
        return positive[0].package
    return None


def _requested_pair(clause: Any) -> tuple[str | None, str, Any] | None:
    """Recover the parent and requested package, including self dependencies."""
    positive = [term for term in clause.terms if term.is_positive()]
    negative = [term for term in clause.terms if not term.is_positive()]
    if len(positive) == 1 and clause.dependency_range is not None:
        parent = positive[0].package
        if isinstance(parent, str):
            return parent, parent, positive[0].constraint
    if len(positive) != 1 or len(negative) != 1:
        return None
    if not isinstance(negative[0].package, str):
        return None
    parent = positive[0].package
    if parent is ROOT or not isinstance(parent, str):
        return None, negative[0].package, None
    return parent, negative[0].package, positive[0].constraint
