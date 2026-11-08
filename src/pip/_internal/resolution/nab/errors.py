"""Pass original requirement provenance back to pip's error renderer."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from pip._vendor.nab_resolver.errors import ResolutionError, ResolutionTerminatedError
from pip._vendor.nab_resolver.proofs import select_dependency_causes
from pip._vendor.nab_resolver.ranges import Range
from pip._vendor.packaging.version import Version

from pip._internal.exceptions import InstallationError
from pip._internal.resolution.nab.base import RequirementCause
from pip._internal.resolution.nab.requirements import UnsatisfiableRequirement

if TYPE_CHECKING:
    from pip._vendor.nab_provider.preference_provider import PreferenceProvider
    from pip._vendor.nab_resolver.types import RangeProtocol
    from pip._vendor.packaging.ranges import VersionRange
    from pip._vendor.packaging.requirements import Requirement as PinDeclaration

    from pip._internal.resolution.nab.catalogue import Candidate, PipCatalogue


def native_requirement_causes(
    catalogue: PipCatalogue,
    *,
    parent: object | None,
    child: object,
    parent_range: RangeProtocol[Version] | None,
) -> Iterator[RequirementCause]:
    """Recover the original requirements represented by one proof cause."""
    if parent is None:
        yield from (
            RequirementCause(req, None)
            for req in catalogue.collected.requirements
            if req.name == child
        )
        return

    assert parent_range is not None
    for choice, data in catalogue.facts.items():
        if (
            data is None
            or choice.package != parent
            or choice.version not in parent_range
        ):
            continue
        for requirement in data.requirements:
            if requirement.name != child:
                continue
            yield RequirementCause(requirement, data.native)

            # A rejected URL still needs its conflicting mandatory source.
            if (
                isinstance(requirement, UnsatisfiableRequirement)
                and requirement.source_requirement is not None
            ):
                yield from (
                    RequirementCause(root, None)
                    for root in catalogue.collected.requirements
                    if root.name == child and root.get_candidate_lookup()[0] is not None
                )


def catalogue_installation_error(
    error: ResolutionError,
    proxy: PreferenceProvider[Candidate, VersionRange, PinDeclaration],
    catalogue: PipCatalogue,
) -> Exception:
    """Render a prepared-query failure using Pip's original declarations."""
    if isinstance(error, ResolutionTerminatedError):
        return InstallationError(str(error))

    selected = select_dependency_causes(
        proxy.project_requirements(
            error.incompatibility,
            version_of=lambda candidate: candidate.version,
            range_type=Range[Version],
            left_first=False,
        )
    )
    records = [
        record
        for cause in selected
        for record in native_requirement_causes(
            catalogue,
            parent=cause.parent,
            child=cause.package,
            parent_range=cause.parent_range,
        )
    ]
    if not records:
        message = "no solution satisfies the requirements"
        if catalogue.rejections:
            message += f"\nLast metadata rejection: {catalogue.rejections[-1]}"
        return InstallationError(message)
    return catalogue.factory.get_installation_error(
        records, catalogue.collected.constraints
    )
