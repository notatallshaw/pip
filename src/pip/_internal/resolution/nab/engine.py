"""The seam between the pip adapter and nab.

This is the only module in the adapter that imports nab, apart from
:mod:`.fetch_port`, which implements nab's own fetch interface.

What it binds to. nab ships two resolvers: ``nab_resolver``, a generic
packaging-free PubGrub solver, and ``nab_provider``, the PyPI provider that
drives it. This seam takes both. pip builds ``nab_provider.provider.Provider``
directly and hands it a fetch port, so nab keeps the candidate scan, the
metadata ladder, the decision priority key, the range widening, the yank
rule, the prerelease admission, the extras proxies and the look-ahead.
What pip supplies is the index behind the port and the facts only pip has:
which versions are yanked, which requirement pins one, which installed
version should be tried first, and which versions need no build.

What it does not bind to is ``nab_project._resolve.engine``.
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

from pip._vendor.nab_provider.diagnostics import BlockerKind, NoVersionsKind
from pip._vendor.nab_provider.provider import (
    ExtrasMode,
    MetadataError,
    join_extra,
    split_extra,
)
from pip._vendor.nab_provider.provider import Provider as NabProvider
from pip._vendor.nab_resolver.errors import ResolutionError
from pip._vendor.nab_resolver.resolver import Resolver as NabResolver
from pip._vendor.nab_resolver.root import ROOT
from pip._vendor.packaging.ranges import VersionRange
from pip._vendor.packaging.specifiers import SpecifierSet
from pip._vendor.packaging.utils import canonicalize_name

from pip._internal.exceptions import InstallationError
from pip._internal.models.link import links_equivalent
from pip._internal.resolution.model.base import format_name
from pip._internal.resolution.nab.errors import (
    FailureCause,
    RejectionBlocker,
    causes_from_derivation,
)
from pip._internal.resolution.nab.fetch_port import PipFetchPort
from pip._internal.resolution.nab.inputs import split_key

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pip._vendor.nab_resolver.types import RangeProtocol
    from pip._vendor.packaging.utils import NormalizedName
    from pip._vendor.packaging.version import Version

    from pip._internal.models.link import Link
    from pip._internal.resolution.nab.candidates import PipHostIndex
    from pip._internal.resolution.nab.inputs import ResolveInputs, RootRequirement
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

    def yanked_versions(
        self, package: str, candidates: Sequence[Version], /
    ) -> frozenset[Version]:
        return self._index.yanked_among(canonicalize_name(package), candidates)

    def admits_yanked(self, package: str, /, *, all_yanked: bool) -> bool:
        return all_yanked and canonicalize_name(package) in self._pinned


class PreferencePolicy:
    """The installed environment, as the version nab tries before it lists.

    pip's own resolver expresses "prefer what is installed" by putting the
    installed candidate first in a lazy candidate sequence and touching the
    index only if the solver asks for more
    (``resolution/model/found_candidates.py``). nab expresses it as a
    preferred version plus the metadata to check it against, which makes the
    same resolve ask the index the same number of times: none.
    """

    def __init__(self, index: PipHostIndex, port: PipFetchPort) -> None:
        self._index = index
        self._port = port

    def preferred_version(self, package: str, /) -> Version | None:
        name = canonicalize_name(package)
        if self._index.eligible_for_upgrade(name):
            return None
        candidate = self._index.installed(name)
        return None if candidate is None else candidate.version

    def preferred_metadata(self, package: str, version: Version, /) -> str | None:
        return self._port.offline_metadata(package, version)


class PrereleasePolicy:
    """The two prerelease admissions pip has that no requirement states.

    Release control is the user saying yes, for one package or for all of
    them. The installed distribution is pip asking only whether what is
    already there still fits. nab owns neither rule; it asks for the facts
    where it filters.

    Only the admitting side is here. ``--only-final`` is applied to the
    universe by ``CandidateEvaluator.rank_candidates``, which is where a
    refusal belongs: a universe with no pre-release in it cannot admit one,
    and a pre-release the user named by URL or by an exact pin never goes
    through that filter.
    """

    def __init__(self, index: PipHostIndex) -> None:
        self._index = index

    def admits_prereleases(self, package: str, /) -> bool:
        return self._index.allows_prereleases(canonicalize_name(package))

    def admitted_prereleases(self, package: str, /) -> frozenset[Version]:
        return self._index.installed_versions(canonicalize_name(package))


class _PreferBinaryProvider(NabProvider):
    """nab's provider with pip's ``--prefer-binary`` ordering on top.

    ``binary_preference`` sits above the version in
    ``CandidateEvaluator._sort_key``, so pip tries a 0.8 wheel before a 1.0
    source archive. nab orders candidates by version alone, and the listing
    is the wrong place to say otherwise: the same order backs the widening
    universe, which has to stay version-monotonic.

    :meth:`selectable_versions` is where the two meet. nab hands it the
    versions left in the range being chosen from, in the order the strategy
    will read them, which is exactly the set pip applies the preference to
    in ``_iter_found_candidates``. Reordering is not filtering, so every
    version stays selectable and only the order they are tried in moves.
    """

    def __init__(self, *args: Any, index: PipHostIndex, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._index = index

    def selectable_versions(
        self, normalized: str, candidates: list[Version]
    ) -> list[Version]:
        selectable = super().selectable_versions(normalized, candidates)
        if not self._index.prefers_binary():
            return selectable
        binary = self._index.binary_versions(canonicalize_name(normalized))
        # Stable, so each group keeps the version order nab handed over.
        return sorted(selectable, key=lambda version: version not in binary)


def solve(
    *,
    inputs: ResolveInputs,
    index: PipHostIndex,
    reporter: NabReporter,
    yank_policy: YankPolicy,
    prerelease_policy: PrereleasePolicy,
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
    provider = _PreferBinaryProvider(
        port,
        index=index,
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
        prerelease_policy=prerelease_policy,
        preference_policy=PreferencePolicy(index, port),
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
        raise _failure(exc, provider, port, inputs, index) from exc
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

    Mirrors ``nab_provider.resolver_inputs.build_resolver_inputs``: one range
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
    index: PipHostIndex,
) -> EngineFailure:
    reader = _DerivationReader(provider, port, inputs, index)
    causes = causes_from_derivation(
        exc.incompatibility,
        root_sentinel=ROOT,
        root_causes=reader.root_causes,
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
        self,
        provider: NabProvider,
        port: PipFetchPort,
        inputs: ResolveInputs,
        index: PipHostIndex,
    ) -> None:
        self._provider = provider
        self._port = port
        self._inputs = inputs
        self._index = index

    def root_causes(self, dep_key: str) -> list[FailureCause]:
        """pip's causes for every root requirement on ``dep_key``.

        resolvelib reports a conflict as every requirement recorded against
        the node, not just the ones the proof used, and pip prints one line
        per entry. nab's root input is one range per node, so the proof can
        only name the intersection; the requirements folded into it are read
        back from what pip collected. A node with no root requirement is a
        dependency clause's target, which never reaches here.
        """
        key, extras = self._reported_node(dep_key)
        roots = self._inputs.roots_for(key)
        if not roots:
            return [FailureCause(requirement=dep_key)]
        return [
            FailureCause(
                requirement=root.text,
                explicit_root=root if root.link is not None else None,
                node_extras=extras,
            )
            for root in self._recorded_roots(roots)
        ]

    def _recorded_roots(
        self, roots: Sequence[RootRequirement]
    ) -> Sequence[RootRequirement]:
        """The root requirements pip would have recorded before giving up.

        resolvelib adds root requirements one at a time and stops at the
        first one that leaves the package with no candidate, so the report
        names that requirement and the ones ahead of it, not every
        requirement the user wrote. ``pip install "pkg>1" "pkg==1.0"`` names
        both, because ``>1`` still has 2.0; a lock file pinning 2.0 plus a
        command line asking for ``<2`` names only ``<2``, because the pin
        leaves 2.0 as the only candidate and ``<2`` rules it out on its own.

        nab folds every root into one range before the solve, so the stopping
        point is recovered by replaying the intersection over the candidates
        pip would have searched. Two requirements naming different URLs are
        the one case the candidates cannot answer: each names its own
        distribution, and the second is what makes them irreconcilable.
        """
        links: list[Link] = []
        specifier = SpecifierSet()
        for count, root in enumerate(roots, start=1):
            if root.link is not None:
                if not any(links_equivalent(link, root.link) for link in links):
                    links.append(root.link)
                if len(links) > 1:
                    return roots[:count]
                continue
            specifier &= root.specifier
            if links:
                continue
            if not self._admits(roots[0].project_name, specifier):
                return roots[:count]
        return roots

    def _admits(self, project_name: NormalizedName, specifier: SpecifierSet) -> bool:
        """Whether any candidate pip can see satisfies ``specifier``."""
        return any(
            specifier.contains(candidate.version, prereleases=True)
            for candidate in self._index.candidates(project_name)
        )

    def _reported_node(self, dep_key: str) -> tuple[str, frozenset[NormalizedName]]:
        """The node pip would have reported ``dep_key`` against.

        nab keys an extras proxy as a package of its own and decides it
        before its base, so a project left with no version is blamed on the
        proxy. It is one distribution, the proxy's candidates are the base's,
        and pip records a URL or a specifier against the base, so the base is
        the node whose requirements the message is about. A proxy whose base
        carries no root requirement of its own is what the user named
        directly, and it stays the node.
        """
        project_name, extras = split_key(dep_key)
        if extras and self._inputs.roots_for(project_name):
            return project_name, frozenset()
        return dep_key, extras

    def requirement_text(self, parent_key: str, dep_key: str) -> str | None:
        """The dependency as written, for the clause naming ``dep_key``."""
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
        parent_name = str(canonicalize_name(split_extra(parent_key)[0]))
        recorded = sorted(
            version
            for (name, version) in self._provider.deps_cache
            if name == parent_name and version in parent_range  # type: ignore[operator]
        )
        if recorded:
            return recorded
        # Nothing was recorded, which is the case where every candidate was
        # refused before its dependencies were read: an unreadable
        # distribution, or one whose own Requires-Python excludes the target.
        # pip still names a version, so the newest listed one answers.
        listed = [
            version
            for version, _dist in self._provider.versions_cache.get(parent_name, ())
            if version in parent_range  # type: ignore[operator]
        ]
        return sorted(listed)[-1:]

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
