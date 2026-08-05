"""Turn an engine failure into the error message pip already prints.

pip's ``Factory.get_installation_error`` is 236 lines of fixed sentences
that a large number of tests assert on, so it is reused unchanged. It wants
``(requirement, parent)`` pairs of pip's own ``Requirement`` and
``Candidate`` objects. PubGrub instead produces a derivation tree, so this
module rebuilds pip objects from a flat description of that tree.

Splitting it this way keeps the part that needs nab as small as it can be:
walking the derivation tree down to a list of :class:`FailureCause` is the
only piece left, and it is the only piece that has to know nab's shapes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pip._vendor.resolvelib import ResolutionImpossible
from pip._vendor.resolvelib.structs import RequirementInformation

from pip._internal.resolution.nab.inputs import split_key

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pip._vendor.packaging.specifiers import SpecifierSet
    from pip._vendor.packaging.utils import NormalizedName
    from pip._vendor.packaging.version import Version

    from pip._internal.exceptions import InstallationError
    from pip._internal.resolution.nab.candidates import PipHostIndex
    from pip._internal.resolution.nab.inputs import ResolveInputs
    from pip._internal.resolution.resolvelib.base import Candidate, Requirement
    from pip._internal.resolution.resolvelib.factory import Factory


DERIVATION_MISSING = (
    "The nab resolver cannot explain this failure yet: turning nab's "
    "derivation tree into pip's (requirement, parent) causes needs nab "
    "vendored. What is missing is the walk from the terminal incompatibility "
    "down to the root causes; everything after that walk is written."
)


@dataclass(frozen=True)
class FailureCause:
    """One requirement the engine could not satisfy, and who wanted it.

    ``requirement`` is a PEP 508 string. ``parent_key`` is None when the user
    asked for it directly, which is the case pip reports as "The user
    requested".
    """

    requirement: str
    parent_key: str | None = None
    parent_project_name: NormalizedName | None = None
    parent_version: Version | None = None
    # Set instead of ``requirement`` when the unsatisfiable thing is the
    # running interpreter's version. pip reports that case first and with a
    # different sentence.
    requires_python: SpecifierSet | None = None


def causes_from_derivation(derivation: object) -> Sequence[FailureCause]:
    """Flatten the engine's derivation tree into pip's causes.

    This is the one piece of the error path that has to know nab's shapes.
    """
    raise NotImplementedError(DERIVATION_MISSING)


def to_installation_error(
    causes: Sequence[FailureCause],
    *,
    factory: Factory,
    index: PipHostIndex,
    inputs: ResolveInputs,
) -> InstallationError:
    """Build the error pip would have raised for the same conflict."""
    assert causes, "Installation error reported with no cause"
    pip_causes = [_cause_pair(cause, factory=factory, index=index) for cause in causes]
    impossible: ResolutionImpossible[Requirement, Candidate] = ResolutionImpossible(
        pip_causes
    )
    return factory.get_installation_error(
        impossible,
        {str(name): constraint for name, constraint in inputs.constraints.items()},
    )


def _cause_pair(
    cause: FailureCause,
    *,
    factory: Factory,
    index: PipHostIndex,
) -> RequirementInformation[Requirement, Candidate]:
    parent = _parent_candidate(cause, index=index)
    if cause.requires_python is not None:
        requirement = factory.make_requires_python_requirement(cause.requires_python)
        assert requirement is not None, (
            "Requires-Python cause reported under --ignore-requires-python"
        )
        return RequirementInformation(requirement, parent)

    requirements = list(
        factory.make_requirements_from_spec(
            cause.requirement,
            comes_from=parent.get_install_requirement() if parent else None,
        )
    )
    assert requirements, f"requirement {cause.requirement!r} produced no cause"
    # A specifier plus extras splits in two; the second is the one carrying
    # the extras, which is what pip's message names.
    return RequirementInformation(requirements[-1], parent)


def _parent_candidate(cause: FailureCause, *, index: PipHostIndex) -> Candidate | None:
    if cause.parent_key is None or cause.parent_project_name is None:
        return None
    assert cause.parent_version is not None, "a parent cause must carry a version"
    _, extras = split_key(cause.parent_key)
    for host_candidate in index.candidates(cause.parent_project_name):
        if host_candidate.version == cause.parent_version:
            return index.pip_candidate(host_candidate, extras)
    return None
