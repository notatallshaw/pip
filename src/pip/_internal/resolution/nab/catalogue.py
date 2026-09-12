"""Static version queries with native candidate preparation and final admission."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Mapping

from pip._vendor.nab_resolver.priority import compute_tier
from pip._vendor.nab_resolver.resolver import (
    BaseProvider,
    Resolver,
    ResolverObserver,
    Solution,
)
from pip._vendor.nab_resolver.types import Incompatibility, IncompatibilityCause, Term
from pip._vendor.packaging.ranges import VersionRange
from pip._vendor.packaging.specifiers import SpecifierSet
from pip._vendor.packaging.version import Version

from pip._internal.resolution.nab.base import (
    Candidate,
    CatalogueUnsupported,
    Constraint,
)
from pip._internal.resolution.nab.factory import CollectedRootRequirements, Factory
from pip._internal.resolution.nab.provider import PipProvider
from pip._internal.resolution.nab.reporter import PipDebuggingReporter, PipReporter


def cached_dependency_span(package, version, universe, dependencies):
    """Cover adjacent cached equivalents and the empty gaps around them."""
    below = bisect_left(universe, version)
    above = bisect_right(universe, version)
    cached = dependencies.get((package, version))
    if cached is not None:
        while (
            below
            and dependencies.get((package, universe[below - 1]), ((), None))[1]
            == cached[1]
        ):
            below -= 1
        while (
            above < len(universe)
            and dependencies.get((package, universe[above]), ((), None))[1] == cached[1]
        ):
            above += 1
    return VersionRange.from_bounds(
        universe[below - 1] if below else None,
        universe[above] if above < len(universe) else None,
        include_lower=False,
        include_upper=False,
    )


class CatalogueObserver(ResolverObserver[str, Version]):
    """Forward static solver decisions to pip's candidate reporter."""

    def __init__(self, provider, reporter):
        self.provider = provider
        self.reporter = reporter
        self.decisions = {}

    def on_decision(self, package, version, level):
        self.decisions[package] = version
        self.reporter.pinning(self.provider.candidates[package, version])

    def on_conflict_step(
        self,
        incompatibility,
        *,
        satisfier_package,
        satisfier_is_decision,
        satisfier_level,
        previous_level,
        can_backjump,
    ):
        if satisfier_is_decision and satisfier_package in self.decisions:
            candidate = self.provider.candidates[
                satisfier_package, self.decisions[satisfier_package]
            ]
            self.reporter.rejecting_candidate((), candidate)


class CatalogueProvider(BaseProvider[str, Version]):
    """Keep per-version metadata independent of the active native declarations."""

    def __init__(
        self,
        factory: Factory,
        native: PipProvider,
        collected: CollectedRootRequirements,
    ):
        self.factory = factory
        self.native = native
        self.collected = collected
        self.catalogues = {}
        self.candidates = {}
        self.dependencies = {}
        self.templates = {}
        self.solution_ranges = {}
        self.pending_dependencies = []
        self.matching_counts = {}
        self.universes = {}
        self.roots = self.ranges(collected.requirements, roots=True)
        self.constraints = {}
        for package, constraint in collected.constraints.items():
            if constraint.links or constraint.hashes:
                raise CatalogueUnsupported("source or hash constraint")
            if factory.catalogue_requires_yanked(package, constraint.specifier):
                raise CatalogueUnsupported("yanked constraint pin")
            self.constraints[package] = constraint.specifier.to_range()

    def ranges(self, requirements, *, roots=False):
        """Translate declarations without granting URL candidates eligibility."""
        ranges = {}
        for requirement in requirements:
            candidate, ireq = requirement.get_candidate_lookup()
            if candidate is not None:
                if roots:
                    raise CatalogueUnsupported("explicit root")
                self.remember(candidate)
                allowed = VersionRange.singleton(candidate.version)
            elif ireq is not None:
                if ireq.link or ireq.hash_options or ireq.config_settings:
                    raise CatalogueUnsupported("requirement preparation options")
                if self.factory.catalogue_requires_yanked(
                    requirement.name, ireq.specifier
                ):
                    raise CatalogueUnsupported("yanked requirement pin")
                self.templates.setdefault(requirement.name, ireq)
                allowed = ireq.specifier.to_range()
            else:
                allowed = VersionRange.empty()
            name = requirement.name
            ranges[name] = ranges.get(name, VersionRange.full()) & allowed
        return ranges

    def remember(self, candidate):
        key = candidate.name, candidate.version
        previous = self.candidates.setdefault(key, candidate)
        if previous != candidate:
            raise CatalogueUnsupported("same-version source replacement")

    def catalogue(self, package):
        if package not in self.catalogues:
            self.catalogues[package] = self.factory.catalogue_candidates(
                package, self.templates.get(package)
            )
        return self.catalogues[package]

    def choose_version(self, package, version_range):
        if self.precheck_base_version(package, version_range):
            return None
        if package.startswith("<"):
            return next(
                (
                    version
                    for (name, version) in self.candidates
                    if name == package and version in version_range
                ),
                None,
            )
        catalogue = self.catalogue(package)
        choices = version_range.filter(
            catalogue.candidates,
            key=lambda item: item.version,
            prereleases=self.factory.catalogue_prerelease_policy(package),
        )
        for artifact in choices:
            version = artifact.version
            key = package, version
            if key not in self.candidates:
                candidate = catalogue.prepare(artifact)
                if candidate is None:
                    raise CatalogueUnsupported("artifact preparation rejected")
                self.remember(candidate)
            if self.precheck_dependencies(package, version):
                return None
            return version
        return None

    def receive_partial_solution_hint(self, positive_ranges, decisions):
        self.solution_ranges = positive_ranges

    def precheck_base_version(self, package, version_range):
        """Enforce extras/base version equality before preparing extras metadata."""
        base, bracket, _ = package.partition("[")
        allowed = self.solution_ranges.get(base) if bracket else None
        if allowed is None or version_range.is_subset(allowed):
            return False
        self.pending_dependencies.append(
            Incompatibility(
                [
                    Term(package, ~allowed, positive=True),
                    Term(base, allowed, positive=True),
                ],
                cause=IncompatibilityCause.DEPENDENCY,
            )
        )
        return True

    def precheck_dependencies(self, package, version):
        """Expose a conflicting dependency before committing its parent candidate."""
        for dependency, required in self.get_dependencies(package, version).items():
            if dependency == package:
                continue
            allowed = self.solution_ranges.get(dependency)
            if allowed is not None and allowed.is_disjoint(required):
                parent_range = self.widen_decision(package, version)
                if parent_range is None:
                    parent_range = VersionRange.singleton(version)
                self.pending_dependencies.append(
                    Incompatibility(
                        [
                            Term(package, parent_range, positive=True),
                            Term(dependency, required, positive=False),
                        ],
                        cause=IncompatibilityCause.DEPENDENCY,
                    )
                )
                return True
        return False

    def consume_pending_clauses(self):
        pending, self.pending_dependencies = self.pending_dependencies, []
        return pending

    def has_satisfying_version(self, package, version_range):
        if package.startswith("<"):
            return any(
                name == package and version in version_range
                for name, version in self.candidates
            )
        return any(
            candidate.version in version_range
            for candidate in self.catalogue(package).candidates
        )

    def get_dependencies(self, package, version):
        key = package, version
        if key not in self.dependencies:
            candidate = self.candidates[key]
            requirements = tuple(self.native.get_dependencies(candidate))
            ranges = self.ranges(requirements)
            self.dependencies[key] = requirements, ranges
        return self.dependencies[key][1]

    def prioritize(self, package, version_range, conflict_counts, culprit_counts=None):
        key = package, version_range
        if key not in self.matching_counts:
            if package.startswith("<"):
                count = sum(
                    name == package and version in version_range
                    for name, version in self.candidates
                )
            else:
                count = len(
                    {
                        candidate.version
                        for candidate in self.catalogue(package).candidates
                        if candidate.version in version_range
                    }
                )
            self.matching_counts[key] = count
        tier = compute_tier(
            package,
            conflict_counts.get(package, 0),
            culprit_counts.get(package, 0) if culprit_counts else 0,
            culprit_counts,
        )
        return (
            tier,
            self.matching_counts[key],
            "[" not in package,
            self.collected.user_requested.get(package, float("inf")),
            package,
        )

    def widen_decision(self, package, version):
        if package.startswith("<"):
            return None
        if package not in self.universes:
            self.universes[package] = sorted(
                {candidate.version for candidate in self.catalogue(package).candidates}
            )
        return cached_dependency_span(
            package, version, self.universes[package], self.dependencies
        )

    def solve(
        self, reporter: PipReporter | PipDebuggingReporter
    ) -> Solution[str, Version]:
        reporter.starting()
        return Resolver(
            self,
            range_type=VersionRange,
            root_version=Version("0"),
            observer=CatalogueObserver(self, reporter),
        ).solve(self.roots, self.constraints)

    def validate(self, solution: Solution[str, Version]) -> bool:
        """Recheck native requirements and artifact order for the selected versions."""
        requirements = defaultdict(list)
        for requirement in self.collected.requirements:
            requirements[requirement.name].append(requirement)
        for name, version in solution.pins.items():
            for requirement in self.dependencies[name, version][0]:
                requirements[requirement.name].append(requirement)
        for name, version in solution.pins.items():
            candidate = self.candidates[name, version]
            if not all(
                requirement.is_satisfied_by(candidate)
                for requirement in requirements[name]
            ):
                return False
            base = self.collected.constraints.get(
                candidate.project_name, Constraint.empty()
            )
            if not base.is_satisfied_by(candidate):
                return False
            if (
                candidate.version.is_prerelease
                and not self.factory.catalogue_prerelease_admitted(
                    candidate, requirements, base
                )
            ):
                return False
            pinned = Constraint(
                base.specifier & SpecifierSet(f"=={version}"),
                base.hashes,
                base.hash_options,
                base.links,
            )
            choices = self.factory.find_candidates(
                name,
                requirements,
                pinned,
                False,
                lambda requirement, candidate: requirement.is_satisfied_by(candidate),
            )
            if next(iter(choices), None) != candidate:
                return False
        return True

    def selected(self, solution: Solution[str, Version]) -> Mapping[str, Candidate]:
        return {
            name: self.candidates[name, version]
            for name, version in solution.pins.items()
        }
