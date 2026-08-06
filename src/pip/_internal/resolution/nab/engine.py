"""The seam between the pip adapter and the nab engine.

This is the only module in the adapter that imports nab. The other five
are pure pip and are unit tested with no nab in the tree.

What it binds to, and why that is narrower than it looks. nab ships two
resolvers: ``nab_resolver``, a generic packaging-free PubGrub solver, and
``nab_python``, the PyPI provider that drives it. ``nab_python``'s provider
cannot be driven from here: ``Provider.__init__`` takes a
``FetchCoordinator`` and reads eleven fields off a ``NabProjectConfig``,
and under this arm pip owns the index, so neither object exists. The port
that would let a host supply candidates is nab-side work that has not
landed. So the seam binds to ``nab_resolver`` directly and this module
supplies the provider: the candidate scan, the dependency expansion,
extras as proxy packages, the decision priority and the range widening.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pip._vendor.nab_resolver.errors import ResolutionError
from pip._vendor.nab_resolver.resolver import Resolver as NabResolver
from pip._vendor.nab_resolver.root import ROOT
from pip._vendor.packaging.ranges import VersionRange
from pip._vendor.packaging.requirements import InvalidRequirement
from pip._vendor.packaging.utils import canonicalize_name

from pip._internal.exceptions import InstallationError
from pip._internal.resolution.nab.candidates import CandidateUnavailable
from pip._internal.resolution.nab.errors import FailureCause, causes_from_derivation
from pip._internal.resolution.nab.inputs import is_pinned, split_key
from pip._internal.resolution.resolvelib.base import format_name
from pip._internal.utils.packaging import get_requirement

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pip._vendor.nab_resolver.types import Incompatibility, RangeProtocol
    from pip._vendor.packaging.requirements import Requirement as PackagingRequirement
    from pip._vendor.packaging.specifiers import SpecifierSet
    from pip._vendor.packaging.utils import NormalizedName
    from pip._vendor.packaging.version import Version

    from pip._internal.req.req_install import InstallRequirement
    from pip._internal.resolution.nab.candidates import (
        CandidateMetadata,
        HostCandidate,
        PipHostIndex,
    )
    from pip._internal.resolution.nab.inputs import ResolveInputs
    from pip._internal.resolution.nab.observer import NabReporter

logger = logging.getLogger(__name__)

# Mirrors nab's own thresholds (``nab_python/_provider/priority.py``). A
# package the search keeps discarding is decided first inside its conflict
# cluster; a runaway top culprit is decided last.
_CONFLICT_THRESHOLD = 5
_CULPRIT_DEMOTE_THRESHOLD = 5
_TIER_AFFECTED = 0
_TIER_NORMAL = 1
_TIER_CULPRIT = 2


@dataclass(frozen=True)
class ResolvedPin:
    """One package the engine decided on.

    ``key`` is the engine's key, which carries the ``[extras]`` part for an
    extras node. pip needs the split because extras collapse back onto the
    base requirement.
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
    """PEP 592, applied where the merged requirement is known.

    pip's rule is that a yanked version is used only when every candidate
    that satisfies the merged requirement is yanked and that requirement pins
    a single version. Both halves are decided during the search, not before
    it: the set of satisfying candidates depends on the range in play, and
    the merged requirement includes transitive requirements the adapter never
    sees.

    So the policy is passed into the engine rather than applied to the
    universe. The engine calls it once it knows both halves.
    """

    def __init__(self, pinned_on_command_line: frozenset[NormalizedName]) -> None:
        self._pinned = pinned_on_command_line

    def admits_yanked(
        self,
        project_name: NormalizedName,
        *,
        all_yanked: bool,
        merged_specifier: SpecifierSet | None = None,
    ) -> bool:
        """May a yanked version of ``project_name`` be selected?

        :param all_yanked: is every version left in the package's current
            range yanked?
        :param merged_specifier: the requirements merged for this package so
            far. When the engine can supply it this is exactly pip's rule.
            When it cannot, the command line pins are used instead, which
            under-approximates: a pin that arises only from a transitive
            ``==`` requirement is missed and the yanked version is refused
            where pip would take it.
        """
        if not all_yanked:
            return False
        if merged_specifier is not None:
            return is_pinned(merged_specifier)
        return project_name in self._pinned


class PipProvider:
    """``nab_resolver.ResolverProvider`` over pip's candidate universe.

    Package keys are pip's own: a canonical project name, or
    ``name[extra1,extra2]`` for an extras node. An extras node ranges over
    the same versions as its base and depends on the base at exactly the
    version it chose, which is how extras stay a search decision rather than
    a post-processing step.

    Every version this provider hands back came out of ``PipHostIndex``, so
    format control, wheel tags, ``--platform``, release control, hash
    intersection and the built-wheel cache are all pip's and already
    applied.
    """

    def __init__(
        self,
        *,
        index: PipHostIndex,
        inputs: ResolveInputs,
        reporter: NabReporter,
        yank_policy: YankPolicy,
        python_version: Version,
        ignore_requires_python: bool,
        widening: bool,
    ) -> None:
        self._index = index
        self._inputs = inputs
        self._reporter = reporter
        self._yank_policy = yank_policy
        self._python_version = python_version
        self._ignore_requires_python = ignore_requires_python
        self._widening = widening

        self._metadata: dict[tuple[NormalizedName, Version], CandidateMetadata] = {}
        self._unusable: dict[tuple[NormalizedName, Version], str] = {}
        self._requires_python_refused: dict[NormalizedName, dict[Version, SpecifierSet]]
        self._requires_python_refused = {}
        # Every dependency mapping handed to the resolver, so widening can
        # compare neighbours and the error path can name the parent version a
        # dependency clause came from.
        self.deps_cache: dict[str, dict[Version, dict[str, VersionRange]]] = {}
        self.dep_texts: dict[str, dict[Version, dict[str, str]]] = {}
        self._widened: dict[tuple[str, Version], VersionRange] = {}
        self._positive_ranges: Mapping[str, RangeProtocol[Version]] = {}
        # has_satisfying_version must leave no trace: it runs the real scan.
        self._probing = False

    # ---------------------------------------------------------------- scan

    def _versions(self, project_name: NormalizedName) -> Sequence[HostCandidate]:
        """Every selectable version of ``project_name``, oldest first."""
        return self._index.candidates(project_name)

    def _metadata_for(
        self, project_name: NormalizedName, candidate: HostCandidate
    ) -> CandidateMetadata | None:
        """Prepare ``candidate``, or return None and record why not."""
        key = (project_name, candidate.version)
        cached = self._metadata.get(key)
        if cached is not None:
            return cached
        if key in self._unusable:
            return None
        try:
            metadata = self._index.metadata(candidate)
        except CandidateUnavailable as exc:
            self._refuse(project_name, candidate.version, exc.reason)
            return None
        requires_python = metadata.requires_python
        if (
            requires_python is not None
            and not self._ignore_requires_python
            and not requires_python.contains(self._python_version, prereleases=True)
        ):
            self._requires_python_refused.setdefault(project_name, {})[
                candidate.version
            ] = requires_python
            self._refuse(
                project_name,
                candidate.version,
                f"requires a different Python: {self._python_version} not in "
                f"{str(requires_python)!r}",
            )
            return None
        self._metadata[key] = metadata
        return metadata

    def _refuse(
        self, project_name: NormalizedName, version: Version, reason: str
    ) -> None:
        self._unusable[project_name, version] = reason
        if not self._probing:
            logger.debug("skipping %s %s: %s", project_name, version, reason)

    def _ordered(
        self,
        project_name: NormalizedName,
        version_range: RangeProtocol[Version],
    ) -> list[HostCandidate]:
        """Candidates to try, best first, with the yank rule applied.

        The universe carries yanked versions because PEP 592 makes one
        selectable exactly when the merged requirement pins it, and that is
        not knowable before the search. So the filter is here, where the
        range in play is known.
        """
        in_range = [
            candidate
            for candidate in self._versions(project_name)
            if candidate.version in version_range
        ]
        if not in_range:
            return []
        in_range = self._apply_prerelease_default(project_name, in_range)
        allowed = [candidate for candidate in in_range if not candidate.yanked]
        if not allowed and self._yank_policy.admits_yanked(
            project_name, all_yanked=True
        ):
            allowed = in_range
        allowed.reverse()
        preferred = self._index.preferred_version(project_name)
        if preferred is not None:
            allowed.sort(key=lambda candidate: candidate.version != preferred)
        return allowed

    def _apply_prerelease_default(
        self, project_name: NormalizedName, in_range: list[HostCandidate]
    ) -> list[HostCandidate]:
        """PEP 440's rule: a prerelease is taken only if nothing else fits.

        This has to be applied here and cannot be applied to the universe.
        ``rank_candidates`` deliberately does not run the specifier (that is
        the prerelease trap: an empty ``SpecifierSet`` would strip the very
        prereleases a ``>=1.0b1`` dependency asks for), so it consults
        release control alone and keeps prereleases whenever release control
        has no opinion. Release control having no opinion means "infer from
        the requirement", and the requirement is a range that only the
        search knows. ``SpecifierSet.filter`` states the inference: yield
        prereleases only when no final version matched.
        """
        if self._index.allows_prereleases(project_name) is True:
            return in_range
        final = [
            candidate for candidate in in_range if not candidate.version.is_prerelease
        ]
        return final or in_range

    # ------------------------------------------------- ResolverProvider

    def choose_version(
        self, package: str, version_range: RangeProtocol[Version]
    ) -> Version | None:
        """Pick the best usable version of ``package`` inside ``version_range``.

        The base range steers which version an extras node picks and never
        whether it picks one. It describes the current partial solution, so
        answering None on its strength would have the resolver record a
        ``NO_VERSIONS`` clause over ``version_range`` that outlives the
        decisions the range came from, and rule out versions that are still
        selectable once those decisions are undone.
        """
        project_name, extras = split_key(package)
        if extras:
            base = self._positive_ranges.get(project_name)
            if base is not None:
                alongside = self._first_usable(project_name, version_range & base)
                if alongside is not None:
                    return alongside
        return self._first_usable(project_name, version_range)

    def _first_usable(
        self, project_name: NormalizedName, version_range: RangeProtocol[Version]
    ) -> Version | None:
        """The best version in ``version_range`` whose metadata reads."""
        for candidate in self._ordered(project_name, version_range):
            if self._metadata_for(project_name, candidate) is not None:
                return candidate.version
        return None

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[Version]
    ) -> bool:
        """Would ``choose_version`` answer, with nothing recorded?"""
        was_probing = self._probing
        self._probing = True
        try:
            return self.choose_version(package, version_range) is not None
        finally:
            self._probing = was_probing

    def get_dependencies(
        self, package: str, version: Version
    ) -> Mapping[str, VersionRange]:
        """What ``package`` at ``version`` needs, as resolver keys and ranges."""
        cached = self.deps_cache.get(package, {}).get(version)
        if cached is not None:
            return cached
        project_name, extras = split_key(package)
        candidate = self._candidate(project_name, version)
        metadata = self._metadata_for(project_name, candidate)
        assert metadata is not None, (
            f"the resolver decided {package} {version}, which has no usable metadata"
        )

        ranges: dict[str, VersionRange] = {}
        texts: dict[str, str] = {}
        if extras:
            ranges[project_name] = VersionRange.singleton(version)
            texts[project_name] = f"{project_name}=={version}"
            self._warn_missing_extras(project_name, version, extras, metadata)
        if not self._inputs.ignore_dependencies:
            comes_from = self._index.pip_candidate(
                candidate, extras
            ).get_install_requirement()
            for requirement in self._requirements(metadata, extras):
                self._add_dependency(requirement, ranges, texts, comes_from)

        self.deps_cache.setdefault(package, {})[version] = ranges
        self.dep_texts.setdefault(package, {})[version] = texts
        return ranges

    def begin_decision_scan(self) -> None:
        """No state moves between scans: this provider is synchronous."""

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[Version],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> tuple[int, int, bool]:
        """``(tier, matching, is_base)``, lower first.

        The same key nab's own provider builds. An extras node sorts before
        its base at equal tier so it pins the base version directly instead
        of provoking a backtrack storm.
        """
        project_name, extras = split_key(package)
        affected = conflict_counts.get(project_name, 0)
        culprit = 0 if culprit_counts is None else culprit_counts.get(project_name, 0)
        tier = self._tier(project_name, affected, culprit, culprit_counts)
        matching = sum(
            1
            for candidate in self._versions(project_name)
            if candidate.version in version_range
        )
        return (tier, matching, not extras)

    @staticmethod
    def _tier(
        project_name: NormalizedName,
        affected: int,
        culprit: int,
        culprit_counts: Mapping[str, int] | None,
    ) -> int:
        if affected >= _CONFLICT_THRESHOLD:
            return _TIER_AFFECTED
        if culprit_counts is None or culprit < _CULPRIT_DEMOTE_THRESHOLD:
            return _TIER_NORMAL
        second = max(
            (count for other, count in culprit_counts.items() if other != project_name),
            default=0,
        )
        if culprit - second >= _CULPRIT_DEMOTE_THRESHOLD:
            return _TIER_CULPRIT
        return _TIER_NORMAL

    def is_ready(self, package: str) -> bool:
        """Always: this provider fetches on demand, in the calling thread."""
        return True

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[str, RangeProtocol[Version]],
        decisions: Mapping[str, Version],
    ) -> None:
        """Keep the base ranges, so an extras node cannot outrun its base.

        An extras node and its base are two packages to the search but one
        distribution in the answer, and the node's only dependency on the
        base is ``base == chosen``. Picking a version the base's own range
        has already ruled out therefore guarantees a conflict on the next
        propagation. pip does not pay that: the extra rides on a candidate
        that was picked once. Reading the base's accumulated range here
        costs nothing and removes the whole class of wasted decision.
        """
        self._positive_ranges = positive_ranges

    def consume_pending_clauses(self) -> list[Incompatibility[str, Version]]:
        """No clauses are queued: this provider has no look-ahead."""
        return []

    def consume_force_backtrack_targets(self) -> list[str]:
        """No force-backtrack signal: this provider has no look-ahead."""
        return []

    def widen_decision(self, package: str, version: Version) -> VersionRange | None:
        """The widened stand-in for ``version`` in dependency clauses.

        A dependency clause names the parent by a range rather than by one
        version, so one clause can rule out a whole run of versions that
        declare the same dependencies. The span runs over adjacent listed
        versions whose recorded dependency maps are equal, then out to the
        open gap around that span, which is what keeps every selectable
        version inside carrying exactly the dependencies being recorded.
        """
        if not self._widening:
            return None
        project_name, extras = split_key(package)
        universe = [candidate.version for candidate in self._versions(project_name)]
        if not universe:
            return None
        key = (package, version)
        cached = self._widened.get(key)
        if cached is not None:
            return cached
        try:
            index = universe.index(version)
        except ValueError:  # pragma: no cover - a decided version is listed
            return None
        below = index
        above = index + 1
        if not extras:
            # Extras nodes keep the plain neighbour gap: their dependency
            # set is per-extra-context, so equality with a neighbour's base
            # map says nothing.
            recorded = self.deps_cache.get(package, {})
            deps = recorded.get(version)
            if deps is not None:
                while below and recorded.get(universe[below - 1]) == deps:
                    below -= 1
                while above < len(universe) and recorded.get(universe[above]) == deps:
                    above += 1
        lower = universe[below - 1] if below else None
        upper = universe[above] if above < len(universe) else None
        widened = VersionRange.from_bounds(
            lower, upper, include_lower=False, include_upper=False
        )
        self._widened[key] = widened
        return widened

    def narrow_for_display(
        self, package: object, constraint: RangeProtocol[Version]
    ) -> RangeProtocol[Version]:
        """Map a widened range back onto versions that exist, for the message."""
        if not isinstance(package, str):
            return constraint
        project_name, _ = split_key(package)
        universe = [candidate.version for candidate in self._versions(project_name)]
        if not universe or not isinstance(constraint, VersionRange):
            return constraint
        if all(version in constraint for version in universe):
            return VersionRange.full(admit_arbitrary=False)
        return constraint.snap_bounds(universe)

    # --------------------------------------------------------- internals

    def _candidate(
        self, project_name: NormalizedName, version: Version
    ) -> HostCandidate:
        for candidate in self._versions(project_name):
            if candidate.version == version:
                return candidate
        raise AssertionError(
            f"the engine decided {project_name} {version}, which is not in the "
            "candidate universe pip supplied"
        )

    def _requirements(
        self, metadata: CandidateMetadata, extras: frozenset[NormalizedName]
    ) -> list[PackagingRequirement]:
        """The dependencies that apply, with pip's own marker semantics.

        ``BaseDistribution.iter_dependencies`` cannot be reused (it drops
        every ``; extra == "x"`` line), but its marker rule is copied here
        verbatim: the base node evaluates against ``extra == ""`` and an
        extras node against one context per requested extra. A line the base
        already carries is left to the base, because the extras node depends
        on the base at its exact version.
        """
        contexts = [{"extra": extra} for extra in sorted(extras)]
        applicable: list[PackagingRequirement] = []
        for text in metadata.raw_dependencies:
            try:
                requirement = get_requirement(text.strip())
            except InvalidRequirement as exc:
                raise InstallationError(
                    f"{metadata.project_name} {metadata.version} declares an "
                    f"invalid dependency {text.strip()!r}: {exc}"
                ) from exc
            marker = requirement.marker
            if not extras:
                if marker is None or marker.evaluate({"extra": ""}):
                    applicable.append(requirement)
                continue
            if marker is None or marker.evaluate({"extra": ""}):
                continue
            if any(marker.evaluate(context) for context in contexts):
                applicable.append(requirement)
        return applicable

    def _add_dependency(
        self,
        requirement: PackagingRequirement,
        ranges: dict[str, VersionRange],
        texts: dict[str, str],
        comes_from: InstallRequirement | None,
    ) -> None:
        name = canonicalize_name(requirement.name)
        extras = frozenset(canonicalize_name(extra) for extra in requirement.extras)
        self._index.note_requested_by(name, requirement.name, comes_from)
        if requirement.url is not None:
            # PEP 508 forbids a specifier alongside a URL, so the link is the
            # whole universe and the range is unbounded.
            if not self._index.register_explicit(str(requirement), comes_from):
                raise InstallationError(
                    f"Cannot install {requirement}: {name} was already "
                    "resolved from the index before this direct URL "
                    "requirement was reached."
                )
            term = VersionRange.full(admit_arbitrary=False)
        else:
            term = (
                requirement.specifier.to_range()
                if requirement.specifier
                else VersionRange.full(admit_arbitrary=False)
            )
        previous = ranges.get(name)
        ranges[name] = term if previous is None else previous & term
        texts[name] = str(requirement)
        if extras:
            key = format_name(name, extras)
            ranges.setdefault(key, VersionRange.full(admit_arbitrary=False))
            texts[key] = str(requirement)

    def _warn_missing_extras(
        self,
        project_name: NormalizedName,
        version: Version,
        extras: frozenset[NormalizedName],
        metadata: CandidateMetadata,
    ) -> None:
        for extra in sorted(extras - metadata.provided_extras):
            logger.warning(
                "%s %s does not provide the extra '%s'", project_name, version, extra
            )

    # ------------------------------------------------------- error path

    def requirement_text(self, parent_key: str | None, dep_key: str) -> str | None:
        """The dependency as written, for the clause naming ``dep_key``."""
        if parent_key is None:
            return self._root_text(dep_key)
        for texts in self.dep_texts.get(parent_key, {}).values():
            text = texts.get(dep_key)
            if text is not None:
                return text
        return None

    def _root_text(self, dep_key: str) -> str | None:
        """The root requirement as the user typed it.

        Not rebuilt from the key: pip's message for ``pip install
        requirements.txt`` keys on the requirement reading exactly
        ``requirements.txt``, and the key is the canonical name.
        """
        for requirement in self._inputs.requirements:
            if requirement.key != dep_key:
                continue
            if requirement.ireq.req is not None:
                return str(requirement.ireq.req)
            return f"{dep_key}{requirement.specifier}"
        return None

    def parent_versions(
        self, parent_key: str, parent_range: RangeProtocol[Version]
    ) -> Sequence[Version]:
        """Every version of ``parent_key`` a clause over ``parent_range`` covers.

        A widened dependency clause names a range rather than one version,
        and the resolver merges clauses that declare the same dependency, so
        one clause can stand for several versions that were each tried. pip
        names each of them, so the versions are recovered here from what was
        actually asked about; a version nothing was recorded for was never
        tried and has nothing to say.
        """
        recorded = sorted(
            version
            for version in self.deps_cache.get(parent_key, {})
            if version in parent_range
        )
        if recorded:
            return recorded
        project_name, _ = split_key(parent_key)
        listed = [
            candidate.version
            for candidate in self._versions(project_name)
            if candidate.version in parent_range
        ]
        return listed[-1:]

    def requires_python_refusal(self, package: str) -> SpecifierSet | None:
        """The Requires-Python that removed every candidate of ``package``."""
        project_name, _ = split_key(package)
        refused = self._requires_python_refused.get(project_name)
        if not refused:
            return None
        return refused[max(refused)]


def solve(
    *,
    inputs: ResolveInputs,
    index: PipHostIndex,
    reporter: NabReporter,
    yank_policy: YankPolicy,
    python_version: Version,
    ignore_requires_python: bool = False,
    widening: bool = True,
) -> Solution:
    """Run the engine over ``inputs``, sourcing versions from ``index``.

    :param widening: keep nab's range widening on. It is worth 13 to 15
        percent of resolve time and it can change which of several valid
        solutions is returned, so it is a behaviour switch and not only a
        performance one.
    :raises EngineFailure: no solution exists. The exception carries the
        causes pip's own error renderer wants.
    """
    provider = PipProvider(
        index=index,
        inputs=inputs,
        reporter=reporter,
        yank_policy=yank_policy,
        python_version=python_version,
        ignore_requires_python=ignore_requires_python,
        widening=widening,
    )
    requirements = _root_ranges(inputs)
    constraints = _constraint_ranges(inputs)
    resolver: NabResolver[str, Version] = NabResolver(
        provider,
        observer=_Observer(reporter),
        range_type=VersionRange,
        root_version="0",
    )
    try:
        solution = resolver.solve(requirements, constraints)
    except ResolutionError as exc:
        raise _failure(exc, provider) from exc

    pins = tuple(_pin(key, version) for key, version in solution.pins.items())
    edges: list[tuple[str | None, str]] = [(None, root) for root in solution.roots]
    edges.extend(solution.edges)
    return Solution(pins=pins, edges=tuple(edges), roots=solution.roots)


def _pin(key: str, version: Version) -> ResolvedPin:
    project_name, extras = split_key(key)
    return ResolvedPin(
        key=key, project_name=project_name, extras=extras, version=version
    )


def _root_ranges(inputs: ResolveInputs) -> dict[str, VersionRange]:
    """One range per root key, in command line order."""
    ranges: dict[str, VersionRange] = {}
    for requirement in inputs.requirements:
        term = (
            requirement.specifier.to_range()
            if requirement.specifier
            else VersionRange.full(admit_arbitrary=False)
        )
        previous = ranges.get(requirement.key)
        ranges[requirement.key] = term if previous is None else previous & term
    return ranges


def _constraint_ranges(inputs: ResolveInputs) -> dict[str, VersionRange]:
    """Constraint ranges, copied onto the extras nodes of the same package.

    A constraint restricts a package without requiring it, and the resolver
    looks it up by the key it is deciding, so an extras node needs its own
    entry to stay on the constraint attribution path.
    """
    ranges: dict[str, VersionRange] = {}
    for project_name, constraint in inputs.constraints.items():
        if not constraint.specifier:
            continue
        ranges[project_name] = constraint.specifier.to_range()
    for requirement in inputs.requirements:
        if requirement.key == requirement.project_name:
            continue
        constrained = ranges.get(requirement.project_name)
        if constrained is not None:
            ranges[requirement.key] = constrained
    return ranges


def _failure(exc: ResolutionError, provider: PipProvider) -> EngineFailure:
    causes = causes_from_derivation(
        exc.incompatibility,
        root_sentinel=ROOT,
        requirement_text=provider.requirement_text,
        parent_versions=provider.parent_versions,
        requires_python=provider.requires_python_refusal,
    )
    if not causes:
        # Nothing in the derivation names a requirement pip can rebuild, which
        # is the iteration-limit and stall path. Report nab's own sentence
        # rather than an empty conflict.
        raise InstallationError(str(exc)) from exc
    return EngineFailure(str(exc), causes)


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

    def on_derivation(
        self, package: str, *, positive: bool, cause: Any
    ) -> None:
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
