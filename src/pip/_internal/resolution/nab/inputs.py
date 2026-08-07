"""Turn pip's root ``InstallRequirement`` list into resolver inputs.

Mirrors ``Factory.collect_root_requirements`` and
``Factory._make_requirements_from_install_req``, but produces a neutral
description of the problem instead of pip ``Requirement`` objects.
Nothing here imports nab; the engine seam in :mod:`.engine` maps this onto
nab's own input types.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pip._vendor.packaging.requirements import InvalidRequirement
from pip._vendor.packaging.specifiers import SpecifierSet
from pip._vendor.packaging.utils import canonicalize_name

from pip._internal.exceptions import InstallationError
from pip._internal.req.constructors import install_req_drop_extras
from pip._internal.req.req_install import check_invalid_constraint_type
from pip._internal.resolution.model.base import Constraint, format_name
from pip._internal.utils.packaging import get_requirement

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from pip._vendor.packaging.utils import NormalizedName

    from pip._internal.models.link import Link
    from pip._internal.req.req_install import InstallRequirement

logger = logging.getLogger(__name__)


def is_pinned(specifier: SpecifierSet) -> bool:
    """Does ``specifier`` pin a single version?

    PEP 592: a yanked release is only admissible when the requirement pins a
    version that only a yanked release can satisfy. Copied from
    ``Factory._iter_found_candidates`` so the two resolvers apply one rule.
    """
    for sp in specifier:
        if sp.operator == "===":
            return True
        if sp.operator != "==":
            continue
        if sp.version.endswith(".*"):
            continue
        return True
    return False


@dataclass(frozen=True)
class RootRequirement:
    """One root requirement, as one node in the resolver's problem.

    ``key`` is the resolver key, which carries the ``[extras]`` part when
    there is one. ``project_name`` never does. ``text`` is how pip's own
    ``Requirement`` for this half spells itself, computed here rather than
    rebuilt from the key: pip's hint for ``pip install requirements.txt``
    tests the requirement reading exactly ``requirements.txt``, and the key
    is the canonical name. ``link`` is set when the user named a URL, path,
    VCS ref or editable, which pip reports as the distribution it builds
    rather than as a specifier.
    """

    key: str
    project_name: NormalizedName
    extras: frozenset[NormalizedName]
    specifier: SpecifierSet
    ireq: InstallRequirement
    text: str
    link: Link | None


@dataclass
class ResolveInputs:
    """Everything the engine needs that comes from pip's command line."""

    requirements: list[RootRequirement] = field(default_factory=list)
    constraints: dict[NormalizedName, Constraint] = field(default_factory=dict)
    user_requested: dict[str, int] = field(default_factory=dict)
    # Requirements that name a URL, VCS ref, local path or editable. Under this
    # arm each is a package whose candidate universe is exactly one entry, so
    # the index supplier answers for them and the engine sees a normal package.
    # A list because a user can name two different URLs for one project, which
    # pip resolves to nothing and reports naming both.
    explicit: dict[NormalizedName, list[InstallRequirement]] = field(
        default_factory=dict
    )
    ignore_dependencies: bool = False

    def specifier_for(self, project_name: NormalizedName) -> SpecifierSet:
        """The specifier known for ``project_name`` before the solve starts.

        Root requirements ANDed with any constraint lines. This is what the
        adapter can see; it is NOT the merged specifier pip's resolver sees at
        ``find_matches`` time, which also carries transitive requirements.
        """
        specifier = SpecifierSet()
        for requirement in self.requirements:
            if requirement.project_name == project_name:
                specifier &= requirement.specifier
        constraint = self.constraints.get(project_name)
        if constraint is not None:
            specifier &= constraint.specifier
        return specifier

    def roots_for(self, key: str) -> Sequence[RootRequirement]:
        """Every root requirement recorded against the node named ``key``.

        The resolvelib equivalent is one ``Criterion``'s ``information``, and
        pip's conflict report prints one line per entry. nab folds the ranges
        into one clause per node, so the entries are recovered here rather
        than off the derivation.
        """
        return [root for root in self.requirements if root.key == key]

    def pinned_packages(self) -> frozenset[NormalizedName]:
        """Packages the command line pins, for the PEP 592 yank exception.

        Under-approximates pip: a pin that only arises from a transitive
        requirement is not visible here. See :mod:`.engine`, which is where
        the exact rule has to be applied once nab can hand the merged
        specifier back.
        """
        return frozenset(
            project_name
            for project_name in self._project_names()
            if is_pinned(self.specifier_for(project_name))
        )

    def _project_names(self) -> set[NormalizedName]:
        names = {requirement.project_name for requirement in self.requirements}
        names.update(self.constraints)
        return names


def collect_inputs(
    root_ireqs: Sequence[InstallRequirement],
    *,
    ignore_dependencies: bool,
    name_link: Callable[[InstallRequirement], NormalizedName] | None = None,
) -> ResolveInputs:
    """Split pip's root ireqs into requirements, constraints and explicit links.

    Mirrors ``Factory.collect_root_requirements``: constraint lines are
    ANDed per project, a requirement whose markers do not apply is dropped
    with the same log line, and ``user_requested`` records command line order.

    :param name_link: names an unnamed URL, path or VCS requirement by
        preparing it. ``pip install ./some/path`` gives no name until the
        distribution has been built, so the caller supplies the one piece of
        machinery that can do it.
    """
    inputs = ResolveInputs(ignore_dependencies=ignore_dependencies)

    for index, ireq in enumerate(root_ireqs):
        if ireq.constraint:
            problem = check_invalid_constraint_type(ireq)
            if problem:
                raise InstallationError(problem)
            if not ireq.match_markers():
                continue
            assert ireq.name, "Constraint must be named"
            name = canonicalize_name(ireq.name)
            if name in inputs.constraints:
                inputs.constraints[name] &= ireq
            else:
                inputs.constraints[name] = Constraint.from_ireq(ireq)
            continue

        requirements = list(_root_requirements_from_ireq(ireq, name_link))
        if not requirements:
            continue
        if ireq.user_supplied and requirements[0].key not in inputs.user_requested:
            inputs.user_requested[requirements[0].key] = index
        if ireq.link is not None:
            inputs.explicit.setdefault(requirements[0].project_name, []).append(ireq)
        inputs.requirements.extend(requirements)

    # Put requirements with extras at the end, matching
    # Factory.collect_root_requirements. Python's sort is stable.
    inputs.requirements.sort(key=lambda r: r.key != r.project_name)
    return inputs


def _root_requirements_from_ireq(
    ireq: InstallRequirement,
    name_link: Callable[[InstallRequirement], NormalizedName] | None,
) -> Iterable[RootRequirement]:
    """Zero, one or two root requirements from one ireq.

    Zero when the markers do not apply. Two when the ireq carries extras and
    either a specifier or a link, which is where pip splits it too: the base
    is constrained centrally and the extras ride on top
    (``SpecifierWithoutExtrasRequirement`` plus ``SpecifierRequirement``, or
    the base link candidate plus the extras candidate).
    """
    if not ireq.match_markers():
        logger.info(
            "Ignoring %s: markers '%s' don't match your environment",
            ireq.name,
            ireq.markers,
        )
        return

    if ireq.name:
        project_name = canonicalize_name(ireq.name)
    else:
        assert ireq.link is not None, "an unnamed requirement must carry a link"
        assert name_link is not None, "no way to name an unnamed URL requirement"
        project_name = name_link(ireq)
    extras = frozenset(canonicalize_name(extra) for extra in ireq.extras)
    # An unnamed URL requirement carries no parsed PEP 508 requirement at all,
    # so there is nothing to constrain the version with: the link is the
    # candidate.
    specifier = ireq.specifier if ireq.req is not None else SpecifierSet()

    if extras and (specifier or ireq.link is not None):
        yield RootRequirement(
            key=project_name,
            project_name=project_name,
            extras=frozenset(),
            specifier=specifier,
            ireq=ireq,
            text=_root_text(install_req_drop_extras(ireq), project_name, frozenset()),
            link=ireq.link,
        )
    yield RootRequirement(
        key=format_name(project_name, extras),
        project_name=project_name,
        extras=extras,
        specifier=specifier,
        ireq=ireq,
        text=_root_text(ireq, project_name, extras),
        link=ireq.link,
    )


def _root_text(
    ireq: InstallRequirement,
    project_name: NormalizedName,
    extras: frozenset[NormalizedName],
) -> str:
    """How pip's ``Requirement`` for this half spells itself.

    A link-backed root is normally rendered from the candidate it builds, so
    this is only its fallback. It is the name rather than ``name @ url`` on
    purpose: the fallback runs when the candidate could not be built, and a
    URL-bearing string would send the renderer back to build it again.
    """
    if ireq.link is not None:
        return format_name(project_name, extras)
    if ireq.req is not None:
        return str(ireq.req)
    return format_name(project_name, extras)


def split_key(key: str) -> tuple[NormalizedName, frozenset[NormalizedName]]:
    """Split a resolver key back into its project name and extras."""
    if "[" not in key:
        return canonicalize_name(key), frozenset()
    try:
        parsed = get_requirement(key)
    except InvalidRequirement:
        return canonicalize_name(key), frozenset()
    return (
        canonicalize_name(parsed.name),
        frozenset(canonicalize_name(extra) for extra in parsed.extras),
    )
