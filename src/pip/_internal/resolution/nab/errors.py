"""Pass original requirement provenance back to pip's error renderer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pip._vendor.nab_resolver.candidate_provider import (
    CandidateProvider,
    CandidateRequirement,
)
from pip._vendor.nab_resolver.errors import ResolutionError, ResolutionTerminatedError
from pip._vendor.nab_resolver.proofs import (
    DependencyCause,
    external_clauses,
    select_dependency_causes,
)
from pip._vendor.nab_resolver.types import Incompatibility
from pip._vendor.packaging.ranges import VersionRange

from pip._internal.exceptions import InstallationError
from pip._internal.resolution.nab.base import Constraint, Requirement, RequirementCause
from pip._internal.resolution.nab.factory import Factory
from pip._internal.resolution.nab.host import NativeHost, Request
from pip._internal.resolution.nab.ranges import CandidateKey
from pip._internal.resolution.nab.yank_candidates import Candidate, YankCandidates

if TYPE_CHECKING:
    from pip._vendor.nab_provider.preference_provider import PreferenceProvider
    from pip._vendor.packaging.requirements import Requirement as PinDeclaration

    from pip._internal.resolution.nab.catalogue import CatalogueProvider


def yanked_installation_error(
    error: ResolutionError,
    proxy: PreferenceProvider[Candidate, VersionRange, PinDeclaration],
    catalogue: YankCandidates,
) -> Exception:
    """Render withdrawal-group proofs using the original pip declarations."""
    if isinstance(error, ResolutionTerminatedError):
        return InstallationError(str(error))

    selected = select_dependency_causes(
        proxy.project_requirements(
            error.incompatibility,
            version_of=lambda candidate: candidate.version,
            range_type=VersionRange,
            left_first=False,
        )
    )
    provider = catalogue.provider
    records: list[RequirementCause] = []
    for cause in selected:
        parent, child, parent_range = cause.parent, cause.package, cause.parent_range
        if parent is None:
            records.extend(
                RequirementCause(req, None)
                for req in provider.collected.requirements
                if req.name == child
            )
            continue
        assert parent_range is not None
        for choice, native in catalogue.prepared.items():
            if choice.package != parent or choice.version not in parent_range:
                continue
            requirements, _ = catalogue.dependencies[choice]
            records.extend(
                RequirementCause(req, native)
                for req in requirements
                if req.name == child
            )
    if not records:
        message = (
            "no solution satisfies the requirements "
            "with grounded yanked-file admission; "
            "a withdrawn file requires an active exact pin"
        )
        if catalogue.rejections:
            message += f"\nLast metadata rejection: {catalogue.rejections[-1]}"
        return InstallationError(message)
    return provider.factory.get_installation_error(
        records, provider.collected.constraints
    )


def catalogue_installation_error(
    error: ResolutionError, provider: CatalogueProvider
) -> Exception:
    """Render a closed-catalogue proof using the original pip requirements."""
    if isinstance(error, ResolutionTerminatedError) or error.incompatibility is None:
        return InstallationError(str(error))

    selected = select_dependency_causes(
        external_clauses(error.incompatibility, left_first=False)
    )
    records: list[RequirementCause] = []
    for cause in selected:
        parent, child, parent_range = cause.parent, cause.package, cause.parent_range
        if parent is None:
            records.extend(
                RequirementCause(req, None)
                for req in provider.collected.requirements
                if req.name == child
            )
            continue
        assert parent_range is not None
        for (package, version), (requirements, _) in provider.dependencies.items():
            if package != parent or version not in parent_range:
                continue
            candidate = provider.candidates[package, version]
            records.extend(
                RequirementCause(req, candidate)
                for req in requirements
                if req.name == child
            )
    if not records:
        return InstallationError(str(error))
    return provider.factory.get_installation_error(
        records, provider.collected.constraints
    )


def installation_error(
    error: ResolutionError,
    provider: CandidateProvider[str, CandidateKey],
    factory: Factory,
    constraints: dict[str, Constraint],
) -> Exception:
    """Render native conflict causes, or the solver error when no causes survive."""
    if isinstance(error, ResolutionTerminatedError) or error.incompatibility is None:
        return InstallationError(str(error))

    clauses = external_clauses(error.incompatibility, left_first=False)
    selected = select_dependency_causes(clauses)
    records = _native_causes(provider, clauses, selected)
    if not records:
        return InstallationError(str(error))
    return factory.get_installation_error(records, constraints)


def _native_causes(
    provider: CandidateProvider[str, CandidateKey],
    clauses: list[Incompatibility[str, CandidateKey]],
    selected: list[DependencyCause[str, CandidateKey]],
) -> list[RequirementCause]:
    """Recover native parents from the proof and deduplicate by object identity."""
    root_origins = {
        id(clause.origin) for clause in clauses if clause.cause.name == "ROOT"
    }
    records = []
    seen = set()
    for selected_cause in selected:
        parent, package, parent_range = (
            selected_cause.parent,
            selected_cause.package,
            selected_cause.parent_range,
        )
        if parent is None:
            causes = _root_causes(provider, package, root_origins)
        else:
            assert parent_range is not None
            causes = [
                cause
                for recorded, candidate in provider.recorded_candidates()
                if recorded == parent and candidate.key in parent_range
                for cause in provider.causes_for(recorded, candidate.key)
                if cause.package == package
            ]
        for cause in causes:
            request = cause.origin
            assert isinstance(request, Request)
            key = id(request.requirement), id(request.parent)
            if key not in seen:
                seen.add(key)
                records.append(RequirementCause(request.requirement, request.parent))
    return records


def _root_causes(
    provider: CandidateProvider[str, CandidateKey], package: str, origins: set[int]
) -> list[CandidateRequirement[str, CandidateKey]]:
    """Stop at the first root request pip's native candidate search rejects."""
    host = provider.host
    assert isinstance(host, NativeHost)
    causes = []
    requirements: dict[str, list[Requirement]] = {}
    for cause in provider.roots:
        if cause.package != package or id(cause.origin) not in origins:
            continue
        causes.append(cause)
        request = cause.origin
        assert isinstance(request, Request)
        requirements.setdefault(package, []).append(request.requirement)
        if next(iter(host.provider.find_matches(package, requirements)), None) is None:
            break
    return causes
