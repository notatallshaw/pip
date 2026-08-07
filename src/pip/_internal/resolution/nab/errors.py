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
from typing import TYPE_CHECKING, Any

from pip._internal.resolution.model.base import (
    RequirementInformation,
    ResolutionImpossible,
)
from pip._internal.resolution.model.requirements import UnsatisfiableRequirement
from pip._internal.resolution.nab.candidates import CandidateUnavailable
from pip._internal.resolution.nab.inputs import split_key

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from pip._vendor.packaging.specifiers import SpecifierSet
    from pip._vendor.packaging.utils import NormalizedName
    from pip._vendor.packaging.version import Version

    from pip._internal.exceptions import InstallationError
    from pip._internal.models.link import Link
    from pip._internal.resolution.model.base import Candidate, Requirement
    from pip._internal.resolution.model.factory import Factory
    from pip._internal.resolution.nab.candidates import PipHostIndex
    from pip._internal.resolution.nab.inputs import ResolveInputs, RootRequirement


@dataclass(frozen=True)
class RejectionBlocker:
    """One reason nab's look-ahead refused every candidate of a package.

    nab's provider does not put a look-ahead rejection into the PubGrub
    proof: it refuses the candidate and records a ``NO_VERSIONS`` clause,
    keeping the reason in its own diagnostic record. That is deliberate,
    the record is part of nab's host-facing API, and it means the proof
    alone says "this package ran out" where pip wants "X depends on Y and
    the user asked for a different Y".

    ``package`` is the dependency that did the blocking. ``against_root``
    says the disagreement was with a root requirement, which pip reports
    as a cause of its own.
    """

    package: str
    against_root: bool = False


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
    # Set for a root the user named by URL, path, VCS ref or editable. pip
    # reports such a request as the distribution it builds, version and
    # location included, so the cause carries the root and the renderer
    # rebuilds the candidate rather than parsing ``requirement`` back into
    # something that would have to be built again.
    explicit_root: RootRequirement | None = None
    # The extras of the node this cause is about, which is not the same as
    # the root's own extras: two roots naming different extras of one project
    # both constrain the base node, and pip names the base candidate there.
    node_extras: frozenset[NormalizedName] = frozenset()


def causes_from_derivation(
    derivation: object,
    *,
    root_sentinel: object,
    root_causes: Callable[[str], Sequence[FailureCause]],
    requirement_text: Callable[[str, str], str | None],
    parent_versions: Callable[[str, object], Sequence[Version]],
    requires_python: Callable[[str], SpecifierSet | None],
    blockers: Callable[[str], Sequence[RejectionBlocker]],
) -> Sequence[FailureCause]:
    """Flatten the engine's derivation tree into pip's causes.

    PubGrub explains a failure as a proof: a terminal incompatibility whose
    two causes are themselves incompatibilities, down to external clauses
    that came from a root requirement, a dependency, or a package having no
    version in a range. pip explains it as a flat list of requirements that
    cannot all hold, each with the candidate that wanted it.

    So the walk collects the external clauses and keeps the ones that name a
    requirement:

    - a ``ROOT`` clause is "the user requested X", with no parent;
    - a ``DEPENDENCY`` clause is "P at V depends on X";
    - a ``NO_VERSIONS`` or ``CONSTRAINT`` clause names no requirement at all.
      It says which package ran out, and pip recomputes that half itself
      (it lists the versions it found and why it skipped them). So those
      clauses select which requirements are reported rather than becoming
      reported causes.

    The tree is walked with an explicit stack. It gains a level per conflict,
    so a deeply backtracked resolve overflows Python's recursion limit on a
    recursive walk, and the failure would then be a ``RecursionError`` raised
    while building the error message.

    Nothing here imports nab: an incompatibility is read through ``cause``,
    ``terms``, ``cause_left`` and ``cause_right``, so the walk is testable
    with plain stand-ins.
    """
    if derivation is None:
        return ()

    external = _external_clauses(derivation)
    blamed = {
        _positive_package(clause)
        for clause in external
        if clause.cause.name in {"NO_VERSIONS", "CONSTRAINT"}
    }
    blamed.discard(None)

    rejected: list[FailureCause] = []
    explained: set[str | None] = set()
    for package in sorted(name for name in blamed if name is not None):
        found = _rejection_causes(
            package,
            blockers(package),
            root_causes=root_causes,
            requirement_text=requirement_text,
            parent_versions=parent_versions,
        )
        if found:
            rejected.extend(found)
            explained.add(package)
    blamed -= explained

    requested = [
        pair
        for clause in external
        if clause.cause.name in {"ROOT", "DEPENDENCY"}
        for pair in [_requested_pair(clause, root_sentinel)]
        if pair is not None
    ]
    selected = [pair for pair in requested if pair[1] in blamed]
    if not selected:
        selected = _most_constrained(requested)

    causes: list[FailureCause] = list(rejected)
    seen: set[tuple[str | None, str, object, Link | None]] = set()
    for parent_key, dep_key, parent_range in [] if rejected else selected:
        node_causes: Sequence[FailureCause]
        if parent_key is None:
            # A root clause states the intersection of every requirement the
            # user wrote on this node, so the requirements are recovered from
            # what pip collected. pip prints one line per requirement, and
            # the count is what makes it print the conflict report at all.
            node_causes = root_causes(dep_key)
        else:
            text = requirement_text(parent_key, dep_key) or dep_key
            node_causes = _causes(
                text, parent_key, parent_range, parent_versions=parent_versions
            )
        for cause in node_causes:
            # Two versions of one parent declaring the same dependency are
            # two causes, not one: pip names each version separately. One
            # widened clause stands for all of them, so the versions are
            # recovered here rather than counted off the clauses. Two roots
            # naming different extras of one project are one cause on the
            # base node, which the link keys apart from a second URL.
            link = None if cause.explicit_root is None else cause.explicit_root.link
            fingerprint = (
                parent_key,
                cause.requirement,
                cause.parent_version,
                link,
            )
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            causes.append(cause)

    causes.extend(
        _requires_python_causes(
            blamed, requires_python, parent_versions=parent_versions
        )
    )
    return causes


def _rejection_causes(
    package: str,
    found: Sequence[RejectionBlocker],
    *,
    root_causes: Callable[[str], Sequence[FailureCause]],
    requirement_text: Callable[[str, str], str | None],
    parent_versions: Callable[[str, object], Sequence[Version]],
) -> list[FailureCause]:
    """pip's causes for a package whose candidates look-ahead all refused.

    The blocker names the dependency that did the refusing, so the cause is
    "``package`` at V depends on that dependency", one per version tried,
    which is the sentence pip prints. A disagreement with a root requirement
    also gets the root's own cause, because pip's message names both sides
    of a conflict and only one of them is a dependency.
    """
    causes: list[FailureCause] = []
    versions = parent_versions(package, _EVERY_VERSION)
    project_name, _ = split_key(package)
    for blocker in found:
        text = requirement_text(package, blocker.package)
        if text is None:
            continue
        # The root side first: pip lists what the user asked for above what a
        # dependency wanted.
        if blocker.against_root:
            causes.extend(root_causes(blocker.package))
        causes.extend(
            FailureCause(
                requirement=text,
                parent_key=package,
                parent_project_name=project_name,
                parent_version=version,
            )
            for version in versions
        )
    return causes


def _most_constrained(
    requested: list[tuple[str | None, str, object]],
) -> list[tuple[str | None, str, object]]:
    """The requirements on the one package that two clauses disagree about.

    A conflict with no ``NO_VERSIONS`` clause is two clauses that cannot both
    hold for the same package: the range they leave it is empty, and the
    contradiction is found by propagation before anything asks for a version.
    pip reports such a conflict as the requirements on that package alone, so
    the package with more than one requirement on it is the one to report.
    Falling back to everything would name packages that are only in the proof
    because they are what asked.
    """
    counts: dict[str, int] = {}
    for _, dep_key, _range in requested:
        counts[dep_key] = counts.get(dep_key, 0) + 1
    if not counts:
        return requested
    most = max(counts.values())
    if most < 2:
        return requested
    contested = {key for key, count in counts.items() if count == most}
    return [pair for pair in requested if pair[1] in contested]


def _causes(
    text: str,
    parent_key: str,
    parent_range: object,
    *,
    parent_versions: Callable[[str, object], Sequence[Version]],
) -> list[FailureCause]:
    project_name, _ = split_key(parent_key)
    return [
        FailureCause(
            requirement=text,
            parent_key=parent_key,
            parent_project_name=project_name,
            parent_version=version,
        )
        for version in parent_versions(parent_key, parent_range)
    ]


def _requires_python_causes(
    blamed: set[str | None],
    requires_python: Callable[[str], SpecifierSet | None],
    *,
    parent_versions: Callable[[str, object], Sequence[Version]],
) -> list[FailureCause]:
    """One cause per package whose candidates all wanted another Python.

    pip reports this case first and with its own sentence, and it reports it
    as a property of the package that declared the constraint, so the
    package is its own cause's parent.
    """
    causes: list[FailureCause] = []
    for package in sorted(name for name in blamed if name is not None):
        specifier = requires_python(package)
        if specifier is None:
            continue
        project_name, _ = split_key(package)
        for version in parent_versions(package, _EVERY_VERSION):
            causes.append(
                FailureCause(
                    requirement=package,
                    parent_key=package,
                    parent_project_name=project_name,
                    parent_version=version,
                    requires_python=specifier,
                )
            )
    return causes


class _EveryVersion:
    """Stands in for a range when any recorded version will do."""

    def __contains__(self, version: object) -> bool:
        return True


_EVERY_VERSION = _EveryVersion()


def _external_clauses(derivation: object) -> list[Any]:
    """Every non-derived clause reachable from ``derivation``, once each."""
    stack: list[Any] = [derivation]
    seen: set[int] = set()
    external: list[Any] = []
    while stack:
        node = stack.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        if node.cause.name == "DERIVED":
            stack.append(node.cause_left)
            stack.append(node.cause_right)
            continue
        external.append(node)
    return external


def _positive_package(clause: Any) -> str | None:
    """The package a single-term clause rules out entirely."""
    positive = [term for term in clause.terms if term.is_positive()]
    if len(positive) != 1:
        return None
    package = positive[0].package
    return package if isinstance(package, str) else None


def _requested_pair(
    clause: Any, root_sentinel: object
) -> tuple[str | None, str, object] | None:
    """``(parent_key, required_key, parent_range)`` for a requirement clause.

    A requirement clause is two terms: the requiring side positive, the
    required side negative, which is PubGrub's way of writing "if the parent
    is in this range then the dependency must be in that one". A one-term
    clause is a self dependency and names no new requirement.
    """
    positive = [term for term in clause.terms if term.is_positive()]
    negative = [term for term in clause.terms if not term.is_positive()]
    if len(positive) != 1 or len(negative) != 1:
        return None
    required = negative[0].package
    if not isinstance(required, str):
        return None
    parent = positive[0].package
    if parent is root_sentinel or not isinstance(parent, str):
        return None, required, None
    return parent, required, positive[0].constraint


def to_installation_error(
    causes: Sequence[FailureCause],
    *,
    factory: Factory,
    index: PipHostIndex,
    inputs: ResolveInputs,
) -> InstallationError:
    """Build the error pip would have raised for the same conflict."""
    assert causes, "Installation error reported with no cause"
    pip_causes = [
        pair
        for pair in (
            _cause_pair(cause, factory=factory, index=index) for cause in causes
        )
        if pair is not None
    ]
    assert pip_causes, "Installation error reported with no rebuildable cause"
    impossible = ResolutionImpossible(pip_causes)
    return factory.get_installation_error(
        impossible,
        {str(name): constraint for name, constraint in inputs.constraints.items()},
    )


def _cause_pair(
    cause: FailureCause,
    *,
    factory: Factory,
    index: PipHostIndex,
) -> RequirementInformation | None:
    if cause.explicit_root is not None:
        return RequirementInformation(
            _explicit_requirement(cause, factory=factory, index=index), None
        )

    parent = _parent_candidate(cause, index=index)
    if cause.requires_python is not None:
        requirement = factory.make_requires_python_requirement(cause.requires_python)
        if requirement is None or parent is None:
            # No requirement under --ignore-requires-python, and pip's
            # Requires-Python sentence names the parent, so a cause with no
            # rebuildable parent has nothing to say.
            return None
        return RequirementInformation(requirement, parent)

    # The extras of the parent node have to travel with the requirement: a
    # dependency of ``pkg[ext]`` still carries its ``; extra == "ext"``
    # marker, and pip drops a requirement whose marker does not hold unless
    # it is told which extras are active.
    requested_extras = (
        split_key(cause.parent_key)[1] if cause.parent_key is not None else frozenset()
    )
    requirements = list(
        factory.make_requirements_from_spec(
            cause.requirement,
            comes_from=parent.get_install_requirement() if parent else None,
            requested_extras=sorted(requested_extras),
        )
    )
    assert requirements, f"requirement {cause.requirement!r} produced no cause"
    # A specifier plus extras splits in two; the second is the one carrying
    # the extras, which is what pip's message names.
    return RequirementInformation(requirements[-1], parent)


def _explicit_requirement(
    cause: FailureCause,
    *,
    factory: Factory,
    index: PipHostIndex,
) -> Requirement:
    """pip's requirement for a distribution the user named directly.

    pip reports such a request as the distribution, so the message carries
    the version and where it came from. A distribution that will not build
    has no version to report, and pip says so in its own sentence
    (``UnsatisfiableRequirement``), which is what the fallback is.
    """
    assert cause.explicit_root is not None
    candidate = index.explicit_candidate(cause.explicit_root, cause.node_extras)
    if candidate is None:
        return UnsatisfiableRequirement(cause.explicit_root.project_name)
    return factory.make_requirement_from_candidate(candidate)


def _parent_candidate(cause: FailureCause, *, index: PipHostIndex) -> Candidate | None:
    if cause.parent_key is None or cause.parent_project_name is None:
        return None
    if cause.parent_version is None:
        return None
    _, extras = split_key(cause.parent_key)
    host_candidate = index.find(cause.parent_project_name, cause.parent_version)
    if host_candidate is None:
        return None
    try:
        return index.pip_candidate(host_candidate, extras)
    except CandidateUnavailable:
        # The parent is only named to say who wanted the thing that failed.
        # If it cannot be rebuilt, report the requirement without it rather
        # than losing the whole message.
        return None
