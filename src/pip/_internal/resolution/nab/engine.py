"""The seam between the pip adapter and nab.

This is the only module in the adapter that imports nab, apart from
:mod:`.fetch_port`, which implements nab's own fetch interface.

What it binds to. nab ships two resolvers: ``nab_resolver``, a generic
packaging-free PubGrub solver, and ``nab_python``, the PyPI provider that
drives it. This seam takes both. pip builds ``nab_python.provider.Provider``
directly and hands it a fetch port, so nab keeps the candidate scan, the
metadata ladder, the decision priority key, the range widening, the yank
rule, the prerelease admission, the extras proxies and the look-ahead.
What pip supplies is the index behind the port and the facts only pip has:
which versions are yanked, which requirement pins one, and which installed
version should be tried first.

What it does not bind to is ``nab_python._resolve.engine``.
``_EngineSettings`` requires a ``NabProjectConfig``, whose replacement is a
deliberately deferred redesign, and what the engine adds over the provider
is per-target iteration, marker-set slicing and a lock writer. pip resolves
for one environment and writes its own lock, so it wants none of them, and
going through the engine would cost three more vendored modules.

Key shapes. nab keys an extras proxy ``name[extra]``, one node per extra;
pip keys it ``name[e1,e2]``, one node per requirement. The conversion is at
this boundary and nowhere else: :func:`_root_ranges` splits pip's spelling
into nab's on the way in, and :func:`_pins` merges nab's back into pip's on
the way out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pip._vendor.nab_python._extra_keys import join_extra, split_extra
from pip._vendor.nab_python.diagnostics import BlockerKind, NoVersionsKind
from pip._vendor.nab_python.provider import ExtrasMode, MetadataError
from pip._vendor.nab_python.provider import Provider as NabProvider
from pip._vendor.nab_resolver.errors import ResolutionError
from pip._vendor.nab_resolver.resolver import Resolver as NabResolver
from pip._vendor.nab_resolver.root import ROOT
from pip._vendor.packaging.ranges import VersionRange
from pip._vendor.packaging.utils import canonicalize_name

from pip._internal.exceptions import InstallationError
from pip._internal.resolution.nab.errors import (
    FailureCause,
    RejectionBlocker,
    causes_from_derivation,
)
from pip._internal.resolution.nab.fetch_port import PipFetchPort
from pip._internal.resolution.nab.inputs import split_key
from pip._internal.resolution.resolvelib.base import format_name

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pip._vendor.nab_resolver.types import RangeProtocol
    from pip._vendor.packaging.specifiers import SpecifierSet
    from pip._vendor.packaging.utils import NormalizedName
    from pip._vendor.packaging.version import Version

    from pip._internal.resolution.nab.candidates import PipHostIndex
    from pip._internal.resolution.nab.inputs import ResolveInputs
    from pip._internal.resolution.nab.observer import NabReporter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedPin:
    """One package the engine decided on.

    ``key`` is pip's key, which carries the ``[extras]`` part for an extras
    node. pip needs the split because extras collapse back onto the base
    requirement.
    """

    key: str
    project_name: NormalizedName
    extras: frozenset[NormalizedName]
    version: Version


@dataclass(frozen=True)
class Solution:
    """The engine's answer, in the shape pip consumes it.

    ``edges`` are ``(parent_key, child_key)`` pairs with ``None`` for a
    requirement the user asked for directly. That is the graph
    ``get_topological_weights`` walks, and the ``None`` root is not optional:
    the weighting starts from it.
    """

    pins: tuple[ResolvedPin, ...]
    edges: tuple[tuple[str | None, str], ...]
    roots: tuple[str, ...]


class EngineFailure(Exception):
    """The engine proved no solution exists.

    Carries the causes pip's error renderer wants, already flattened out of
    nab's derivation tree, plus nab's own message for the debug log.
    """

    def __init__(self, message: str, causes: Sequence[FailureCause]) -> None:
        super().__init__(message)
        self.causes = causes


class YankPolicy:
    """The two PEP 592 facts nab asks pip for.

    nab owns the rule ("a yanked version is selectable only when every
    candidate left in range is yanked and the requirement pins one") and
    applies it where both halves are known, inside the selection. Neither
    half is decidable outside it: the set of satisfying candidates depends
    on the range in play, and nab merges requirements into ranges, so a
    range cannot tell ``==1.0`` apart from ``>=1.0,<=1.0``, which pins
    nothing under PEP 592.

    So this class answers facts and nothing else. Which versions the index
    marks yanked comes from pip's own listing, and whether the requirement
    pins one is under-approximated by the command line pins: a pin arising
    only from a transitive ``==`` is missed, and a yanked version is refused
    where pip would take it.
    """

    def __init__(
        self, index: PipHostIndex, pinned_on_command_line: frozenset[NormalizedName]
    ) -> None:
        self._index = index
        self._pinned = pinned_on_command_line

    def yanked_versions(self, package: str, /) -> frozenset[Version]:
        return self._index.yanked_versions(canonicalize_name(package))

    def admits_yanked(self, package: str, /, *, all_yanked: bool) -> bool:
        return all_yanked and canonicalize_name(package) in self._pinned


def solve(
    *,
    inputs: ResolveInputs,
    index: PipHostIndex,
    reporter: NabReporter,
    yank_policy: YankPolicy,
    python_version: Version,
    ignore_requires_python: bool = False,
) -> Solution:
    """Run nab over ``inputs``, sourcing the index through a fetch port.

    :raises EngineFailure: no solution exists. The exception carries the
        causes pip's own error renderer wants.
    """
    port = PipFetchPort(
        host=index,
        python_version=python_version,
        ignore_requires_python=ignore_requires_python,
        ignore_dependencies=inputs.ignore_dependencies,
    )
    requirements, root_extras = _root_ranges(inputs)
    constraints = _constraint_ranges(inputs, root_extras)
    provider = NabProvider(
        port,
        root_requirements=dict(requirements),
        # ``target=None`` turns off every filter pip has already applied:
        # wheel tags, Requires-Python and the upload cutoff. Running them
        # again would double-filter, and where nab's rule differs from pip's
        # it would apply nab's to a universe pip built.
        target=None,
        uploaded_prior_to=None,
        # pip warns and carries on for a missing extra, in its own sentence;
        # nab's default would raise for a user-requested one.
        extras_mode=ExtrasMode.WARN,
        root_extras=root_extras,
        constraints=constraints,
        yank_policy=yank_policy,
        preferences=index.preferences(),
    )
    resolver: NabResolver[str, Version] = NabResolver(
        provider,
        observer=_Observer(reporter),
        range_type=VersionRange,
        root_version="0",
    )
    try:
        solution = resolver.solve(requirements, constraints)
    except ResolutionError as exc:
        raise _failure(exc, provider, port, inputs) from exc
    except MetadataError as exc:
        # Every candidate of a package the search had already committed to
        # turned out to be unreadable. nab reports it as a metadata error
        # rather than as a proof, so there is no derivation to render.
        raise InstallationError(str(exc)) from exc

    edges: list[tuple[str | None, str]] = [(None, root) for root in solution.roots]
    edges.extend(solution.edges)
    return Solution(
        pins=_pins(solution.pins),
        edges=tuple(edges),
        roots=solution.roots,
    )


def _pins(decided: Mapping[str, Version]) -> tuple[ResolvedPin, ...]:
    """nab's per-extra nodes, merged back into pip's per-distribution ones.

    nab decides ``pkg``, ``pkg[a]`` and ``pkg[b]`` as three packages at one
    version; pip installs one distribution with both extras, and its
    ``RequirementSet`` builder wants one candidate carrying ``pkg[a,b]``.
    """
    extras: dict[NormalizedName, set[NormalizedName]] = {}
    versions: dict[NormalizedName, Version] = {}
    for key, version in decided.items():
        base, extra = split_extra(key)
        project_name = canonicalize_name(base)
        versions[project_name] = version
        if extra is not None:
            extras.setdefault(project_name, set()).add(canonicalize_name(extra))

    pins = [
        ResolvedPin(
            key=str(project_name),
            project_name=project_name,
            extras=frozenset(),
            version=version,
        )
        for project_name, version in versions.items()
    ]
    pins.extend(
        ResolvedPin(
            key=format_name(project_name, frozenset(requested)),
            project_name=project_name,
            extras=frozenset(requested),
            version=versions[project_name],
        )
        for project_name, requested in extras.items()
    )
    return tuple(pins)


def _root_ranges(
    inputs: ResolveInputs,
) -> tuple[dict[str, VersionRange], set[tuple[str, str]]]:
    """pip's root requirements in nab's key shape.

    Mirrors ``nab_python._resolve.inputs.build_resolver_inputs``: one range
    per canonical name, intersected across repeats, plus one unbounded entry
    per extra under its own ``name[extra]`` key. ``inputs`` has already
    dropped the requirements whose markers do not apply and named every
    unnamed URL, so neither happens again here.
    """
    ranges: dict[str, VersionRange] = {}
    root_extras: set[tuple[str, str]] = set()
    for requirement in inputs.requirements:
        name = str(requirement.project_name)
        term = (
            requirement.specifier.to_range()
            if requirement.specifier
            else VersionRange.full(admit_arbitrary=False)
        )
        previous = ranges.get(name)
        ranges[name] = term if previous is None else previous & term
        for extra in sorted(requirement.extras):
            ranges[join_extra(name, extra)] = VersionRange.full(admit_arbitrary=False)
            root_extras.add((name, str(extra)))
    return ranges, root_extras


def _constraint_ranges(
    inputs: ResolveInputs, root_extras: set[tuple[str, str]]
) -> dict[str, VersionRange]:
    """Constraint ranges, copied onto the extras proxies of the same package.

    A constraint restricts a package without requiring it, and the resolver
    looks it up by the key it is deciding, so an extras proxy needs its own
    entry to stay on the constraint attribution path.
    """
    ranges: dict[str, VersionRange] = {}
    for project_name, constraint in inputs.constraints.items():
        if not constraint.specifier:
            continue
        ranges[str(project_name)] = constraint.specifier.to_range()
    for name, extra in root_extras:
        constrained = ranges.get(name)
        if constrained is not None:
            ranges[join_extra(name, extra)] = constrained
    return ranges


def _failure(
    exc: ResolutionError,
    provider: NabProvider,
    port: PipFetchPort,
    inputs: ResolveInputs,
) -> EngineFailure:
    reader = _DerivationReader(provider, port, inputs)
    causes = causes_from_derivation(
        exc.incompatibility,
        root_sentinel=ROOT,
        requirement_text=reader.requirement_text,
        parent_versions=reader.parent_versions,
        requires_python=reader.requires_python,
        blockers=reader.blockers,
    )
    if not causes:
        # Nothing in the derivation names a requirement pip can rebuild, which
        # is the iteration-limit and stall path. Report nab's own sentence
        # rather than an empty conflict.
        raise InstallationError(str(exc)) from exc
    return EngineFailure(str(exc), causes)


class _DerivationReader:
    """What pip's error renderer needs, read back off nab's provider.

    A PubGrub clause names a package and a range. pip's message names a
    requirement as it was written and the parent version that declared it,
    so both are recovered from what nab parsed rather than kept in a second
    record: ``metadata_cache`` holds the ``Requirement`` objects and
    ``deps_cache`` holds every ``(package, version)`` the search asked about.
    """

    def __init__(
        self, provider: NabProvider, port: PipFetchPort, inputs: ResolveInputs
    ) -> None:
        self._provider = provider
        self._port = port
        self._inputs = inputs

    def requirement_text(self, parent_key: str | None, dep_key: str) -> str | None:
        """The dependency as written, for the clause naming ``dep_key``."""
        if parent_key is None:
            return self._root_text(dep_key)
        parent_name = canonicalize_name(split_extra(parent_key)[0])
        dep_name, dep_extra = split_extra(dep_key)
        dep_name = str(canonicalize_name(dep_name))
        for (name, _version), metadata in self._provider.metadata_cache.items():
            if name != parent_name:
                continue
            for requirement in metadata.requires_dist:
                if str(canonicalize_name(requirement.name)) != dep_name:
                    continue
                if dep_extra is not None and dep_extra not in {
                    str(canonicalize_name(extra)) for extra in requirement.extras
                }:
                    continue
                return str(requirement)
        return None

    def _root_text(self, dep_key: str) -> str | None:
        """The root requirement as the user typed it.

        Not rebuilt from the key: pip's message for ``pip install
        requirements.txt`` keys on the requirement reading exactly
        ``requirements.txt``, and the key is the canonical name.
        """
        base, extra = split_extra(dep_key)
        project_name = canonicalize_name(base)
        for requirement in self._inputs.requirements:
            if requirement.project_name != project_name:
                continue
            if extra is not None and canonicalize_name(extra) not in requirement.extras:
                continue
            if requirement.ireq.req is not None:
                return str(requirement.ireq.req)
            return f"{dep_key}{requirement.specifier}"
        return None

    def parent_versions(
        self, parent_key: str, parent_range: object
    ) -> Sequence[Version]:
        """Every version of ``parent_key`` a clause over ``parent_range`` covers.

        A widened dependency clause names a range rather than one version,
        and the resolver merges clauses that declare the same dependency, so
        one clause can stand for several versions that were each tried. pip
        names each of them, so the versions are recovered from what was
        actually asked about; a version nothing was recorded for was never
        tried and has nothing to say.
        """
        parent_name = canonicalize_name(split_extra(parent_key)[0])
        return sorted(
            version
            for (name, version) in self._provider.deps_cache
            if name == parent_name and version in parent_range  # type: ignore[operator]
        )

    def blockers(self, package: str) -> list[RejectionBlocker]:
        """What nab's look-ahead said when it refused every candidate.

        ``get_no_versions_reason`` is nab's host-facing diagnostic, and its
        docstring says a host wording its own messages reads the kind and
        the fields it points at rather than nab's sentence. This is that
        read, narrowed to the two blocker kinds pip has a sentence for: a
        dependency that disagrees with a root requirement, and one that
        disagrees with the partial solution.
        """
        reason = self._provider.get_no_versions_reason(
            str(canonicalize_name(split_extra(package)[0]))
        )
        if reason is None or reason.kind is not NoVersionsKind.ALL_REJECTED:
            return []
        return [
            RejectionBlocker(
                package=blocker.package,
                against_root=blocker.kind is BlockerKind.ROOT_RANGE,
            )
            for blocker in reason.blockers
            if blocker.kind
            in (BlockerKind.ROOT_RANGE, BlockerKind.SOLUTION_RANGE, BlockerKind.DECIDED)
        ]

    def requires_python(self, package: str) -> SpecifierSet | None:
        """The Requires-Python that removed every candidate of ``package``."""
        refused = self._port.requires_python_refused.get(
            canonicalize_name(split_extra(package)[0])
        )
        if not refused:
            return None
        return refused[max(refused)]


class _Observer:
    """Forwards nab's resolver events to pip's reporter.

    ``PIP_RESOLVER_DEBUG`` is decided once here rather than per event, so a
    run without it builds no argument tuples.

    The 1st, 8th and 13th backtracking messages are driven from the conflict
    loop, not from a version being unusable. pip prints them from
    ``rejecting_candidate``, which fires when a candidate that was already
    pinned is discarded because of a conflict. The PubGrub event with that
    meaning is a conflict step whose satisfier is a decision: that decision
    is about to be undone. A candidate skipped because its metadata could
    not be read is not that event, and pip does not count one either.
    """

    def __init__(self, reporter: NabReporter) -> None:
        self._reporter = reporter
        self._debug = reporter.event_enabled()
        self._decided: dict[str, Version] = {}
        self._counted: dict[str, Version] = {}

    def on_decision(self, package: str, version: Version, level: int) -> None:
        self._decided[package] = version
        if self._debug:
            self._reporter.event("adding_requirement", package, version, level)

    def on_derivation(self, package: str, *, positive: bool, cause: Any) -> None:
        if self._debug:
            self._reporter.event("derivation", package, positive)

    def on_conflict(self, incompatibility: Any) -> None:
        if self._debug:
            self._reporter.event("resolving_conflicts", incompatibility)

    def on_learned(self, incompatibility: Any) -> None:
        if self._debug:
            self._reporter.event("learned", incompatibility)

    def on_backjump(self, from_level: int, to_level: int) -> None:
        if self._debug:
            self._reporter.event("backtracking", from_level, to_level)

    def on_no_versions(
        self, package: str, version_range: RangeProtocol[Version]
    ) -> None:
        logger.debug("no version of %s left in %s", package, version_range)
        if self._debug:
            self._reporter.event("no_versions", package, str(version_range))

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
        if satisfier_is_decision:
            self._count_rejection(satisfier_package)
        if self._debug:
            self._reporter.event("conflict_step", satisfier_package, satisfier_level)

    def _count_rejection(self, package: str) -> None:
        """One pinned candidate is about to be discarded."""
        version = self._decided.get(package)
        if version is None or self._counted.get(package) == version:
            return
        self._counted[package] = version
        project_name, _ = split_key(package)
        self._reporter.rejecting_version(project_name, f"{package} {version}")
