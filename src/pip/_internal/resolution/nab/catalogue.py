"""Static version queries with native candidate preparation and final admission."""

from __future__ import annotations

import logging
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, cast

from pip._vendor.nab_resolver.priority import compute_tier
from pip._vendor.nab_resolver.resolver import (
    BaseProvider,
    Resolver,
    ResolverObserver,
    Solution,
)
from pip._vendor.nab_resolver.types import (
    Incompatibility,
    IncompatibilityCause,
    RangeProtocol,
    Term,
)
from pip._vendor.packaging.ranges import VersionRange
from pip._vendor.packaging.specifiers import SpecifierSet
from pip._vendor.packaging.version import Version

from pip._internal.req.constructors import install_req_extend_extras
from pip._internal.req.req_install import InstallRequirement
from pip._internal.resolution.nab.base import (
    Candidate,
    CatalogueUnsupported,
    Constraint,
    Requirement,
)
from pip._internal.resolution.nab.factory import (
    CandidateCatalogue,
    CollectedRootRequirements,
    Factory,
)
from pip._internal.resolution.nab.provider import PipProvider
from pip._internal.resolution.nab.reporter import PipDebuggingReporter, PipReporter
from pip._internal.utils.packaging import get_requirement

if TYPE_CHECKING:
    DependencyRecord = tuple[tuple[Requirement, ...], dict[str, VersionRange]]

logger = logging.getLogger(__name__)
_REORDER_AFTER_VERSIONS = 8


class _TryRequestedOrder(Exception):
    """Abandon search state without discarding fixed-catalogue metadata."""


def cached_dependency_span(
    package: str,
    version: Version,
    universe: Sequence[Version],
    dependencies: Mapping[tuple[str, Version], DependencyRecord],
) -> VersionRange:
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

    def __init__(
        self, provider: CatalogueProvider, reporter: PipReporter | PipDebuggingReporter
    ) -> None:
        self.provider = provider
        self.reporter = reporter
        self.decisions: dict[str, Version] = {}

    def on_decision(self, package: str, version: Version, level: int) -> None:
        self.decisions[package] = version
        self.reporter.pinning(self.provider.candidates[package, version])

    def on_conflict_step(
        self,
        incompatibility: Incompatibility[str, Version],
        *,
        satisfier_package: str,
        satisfier_is_decision: bool,
        satisfier_level: int,
        previous_level: int,
        can_backjump: bool,
    ) -> None:
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
    ) -> None:
        self.factory = factory
        self.native = native
        self.collected = collected
        self.catalogues: dict[str, CandidateCatalogue] = {}
        self.installed_versions: dict[str, Version | None] = {}
        self.deferred_yanked: dict[str, list[tuple[SpecifierSet, str]]] = {}
        self.candidates: dict[tuple[str, Version], Candidate] = {}
        self.dependencies: dict[tuple[str, Version], DependencyRecord] = {}
        self.templates: dict[str, InstallRequirement] = {}
        self.solution_ranges: Mapping[str, RangeProtocol[Version]] = {}
        self.pending_dependencies: list[Incompatibility[str, Version]] = []
        self.matching_counts: dict[tuple[str, RangeProtocol[Version]], int] = {}
        self.universes: dict[str, list[Version]] = {}
        self.prepared_counts: dict[str, int] = {}
        self.requested_order = False
        self.roots = self.ranges(collected.requirements, roots=True)
        self.constraints: dict[str, VersionRange] = {}
        for package, constraint in collected.constraints.items():
            if constraint.links or constraint.hashes:
                raise CatalogueUnsupported("source or hash constraint")
            self.deferred_yanked.setdefault(package, []).append(
                (constraint.specifier, "yanked constraint pin")
            )
            self.constraints[package] = constraint.specifier.to_range()

    def ranges(
        self, requirements: Iterable[Requirement], *, roots: bool = False
    ) -> dict[str, VersionRange]:
        """Translate declarations without granting URL candidates eligibility."""
        ranges: dict[str, VersionRange] = {}
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
                self.templates.setdefault(requirement.name, ireq)
                if roots:
                    self.templates.setdefault(requirement.project_name, ireq)
                self.check_yanked(
                    requirement.name, ireq.specifier, "yanked requirement pin"
                )
                allowed = ireq.specifier.to_range()
            else:
                allowed = VersionRange.empty()
            name = requirement.name
            if name != requirement.project_name:
                constraint = self.collected.constraints.get(requirement.project_name)
                if constraint is not None:
                    allowed &= constraint.specifier.to_range()
            ranges[name] = ranges.get(name, VersionRange.full()) & allowed
        return ranges

    def check_yanked(self, package: str, specifier: SpecifierSet, reason: str) -> None:
        """Defer index admission checks while installed metadata may suffice."""
        if (
            package not in self.catalogues
            and self.installed_version(package) is not None
        ):
            self.deferred_yanked.setdefault(package, []).append((specifier, reason))
        elif self.factory.catalogue_requires_yanked(package, specifier):
            raise CatalogueUnsupported(reason)

    def remember(self, candidate: Candidate) -> None:
        key = candidate.name, candidate.version
        previous = self.candidates.setdefault(key, candidate)
        if previous != candidate:
            raise CatalogueUnsupported("same-version source replacement")

    def catalogue(self, package: str) -> CandidateCatalogue:
        """Return the request's fixed artifact list, loading it on first use."""
        if package not in self.catalogues:
            for specifier, reason in self.deferred_yanked.pop(package, ()):
                if self.factory.catalogue_requires_yanked(package, specifier):
                    raise CatalogueUnsupported(reason)
            self.catalogues[package] = self.factory.catalogue_candidates(
                package, self.preparation_template(package)
            )
        return self.catalogues[package]

    def preparation_template(self, package: str) -> InstallRequirement | None:
        """Combine base provenance with the identifier's extras for preparation."""
        base, bracket, _ = package.partition("[")
        template = self.templates.get(base, self.templates.get(package))
        if template is not None and bracket:
            return install_req_extend_extras(template, get_requirement(package).extras)
        return template

    def installed_version(self, package: str) -> Version | None:
        """Cache installed availability independently of finder versions."""
        if package not in self.installed_versions:
            self.installed_versions[package] = self.factory.installed_version(package)
        return self.installed_versions[package]

    def select_installed(self, package: str) -> Version | None:
        """Prepare installed metadata only after choosing its version."""
        candidate = self.factory.installed_candidate(
            package, self.preparation_template(package)
        )
        self.remember(candidate)
        if self.precheck_dependencies(candidate.name, candidate.version):
            return None
        return candidate.version

    def choose_version(
        self, package: str, version_range: RangeProtocol[Version]
    ) -> Version | None:
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
        installed = self.installed_version(package)
        if installed is not None and installed not in version_range:
            installed = None
        if installed is not None and not self.native.eligible_for_upgrade(package):
            return self.select_installed(package)
        catalogue = self.catalogue(package)
        # _solve_once binds this provider to VersionRange's prerelease filtering.
        choices = cast(VersionRange, version_range).filter(
            catalogue.candidates,
            key=lambda item: item.version,
            prereleases=self.factory.catalogue_prerelease_policy(package),
        )
        for artifact in choices:
            version = artifact.version
            if installed is not None and installed >= version:
                return self.select_installed(package)
            key = package, version
            if key not in self.candidates:
                prepared = self.prepared_counts.get(package, 0)
                if (
                    not self.requested_order
                    and prepared >= _REORDER_AFTER_VERSIONS
                    and package not in self.roots
                ):
                    raise _TryRequestedOrder(package)
                candidate = catalogue.prepare(artifact)
                if candidate is None:
                    raise CatalogueUnsupported("artifact preparation rejected")
                self.remember(candidate)
                self.prepared_counts[package] = prepared + 1
            if self.precheck_dependencies(package, version):
                return None
            return version
        return self.select_installed(package) if installed is not None else None

    def receive_partial_solution_hint(
        self,
        positive_ranges: Mapping[str, RangeProtocol[Version]],
        decisions: Mapping[str, Version],
    ) -> None:
        self.solution_ranges = positive_ranges

    def precheck_base_version(
        self, package: str, version_range: RangeProtocol[Version]
    ) -> bool:
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

    def precheck_dependencies(self, package: str, version: Version) -> bool:
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

    def consume_pending_clauses(self) -> list[Incompatibility[str, Version]]:
        pending, self.pending_dependencies = self.pending_dependencies, []
        return pending

    def has_satisfying_version(
        self, package: str, version_range: RangeProtocol[Version]
    ) -> bool:
        if package.startswith("<"):
            return any(
                name == package and version in version_range
                for name, version in self.candidates
            )
        installed = self.installed_version(package)
        if installed is not None and installed in version_range:
            return True
        return any(
            candidate.version in version_range
            for candidate in self.catalogue(package).candidates
        )

    def get_dependencies(
        self, package: str, version: Version
    ) -> dict[str, VersionRange]:
        key = package, version
        if key not in self.dependencies:
            candidate = self.candidates[key]
            requirements = tuple(self.native.get_dependencies(candidate))
            if candidate.is_installed and any(
                requirement.name == package
                and not requirement.is_satisfied_by(candidate)
                for requirement in requirements
            ):
                raise CatalogueUnsupported("installed self replacement")
            ranges = self.ranges(requirements)
            self.dependencies[key] = requirements, ranges
        return self.dependencies[key][1]

    def prioritize(
        self,
        package: str,
        version_range: RangeProtocol[Version],
        conflict_counts: Mapping[str, int],
        culprit_counts: Mapping[str, int] | None = None,
    ) -> (
        tuple[int, bool, int | float, int, bool, str]
        | tuple[int, int, bool, int | float, str]
    ):
        key = package, version_range
        if key not in self.matching_counts:
            self.matching_counts[key] = self.matching_count(package, version_range)
        tier = compute_tier(
            package,
            conflict_counts.get(package, 0),
            culprit_counts.get(package, 0) if culprit_counts else 0,
            culprit_counts,
        )
        if self.requested_order:
            return (
                tier,
                self.matching_counts[key] != 1,
                self.collected.user_requested.get(package, float("inf")),
                self.matching_counts[key],
                "[" not in package,
                package,
            )
        return (
            tier,
            self.matching_counts[key],
            "[" not in package,
            self.collected.user_requested.get(package, float("inf")),
            package,
        )

    def matching_count(
        self, package: str, version_range: RangeProtocol[Version]
    ) -> int:
        """Count known choices, trying a preferred installation before listing."""
        if package.startswith("<"):
            return sum(
                name == package and version in version_range
                for name, version in self.candidates
            )
        installed = self.installed_version(package)
        installed_matches = installed is not None and installed in version_range
        if (
            installed_matches
            and package not in self.catalogues
            and not self.native.eligible_for_upgrade(package)
        ):
            return 1
        versions = {
            candidate.version
            for candidate in self.catalogue(package).candidates
            if candidate.version in version_range
        }
        if installed_matches:
            assert installed is not None
            versions.add(installed)
        return len(versions)

    def widen_decision(self, package: str, version: Version) -> VersionRange | None:
        if package.startswith("<") or package not in self.catalogues:
            return None
        if package not in self.universes:
            versions = {
                candidate.version for candidate in self.catalogue(package).candidates
            }
            installed = self.installed_version(package)
            if installed is not None:
                versions.add(installed)
            self.universes[package] = sorted(versions)
        return cached_dependency_span(
            package, version, self.universes[package], self.dependencies
        )

    def solve(
        self, reporter: PipReporter | PipDebuggingReporter
    ) -> Solution[str, Version]:
        reporter.starting()
        try:
            return self._solve_once(reporter)
        except _TryRequestedOrder as error:
            logger.info(
                "Nab catalogue reordered after repeated preparation of %s", str(error)
            )
            self.requested_order = True
            self.solution_ranges = {}
            self.pending_dependencies.clear()
        return self._solve_once(reporter)

    def _solve_once(
        self, reporter: PipReporter | PipDebuggingReporter
    ) -> Solution[str, Version]:
        """Run a fresh solver over the retained fixed-catalogue metadata."""
        return Resolver(
            self,
            range_type=VersionRange,
            root_version=Version("0"),
            observer=CatalogueObserver(self, reporter),
        ).solve(self.roots, self.constraints)

    def validate(self, solution: Solution[str, Version]) -> bool:
        """Recheck native requirements and artifact order for the selected versions."""
        requirements: dict[str, list[Requirement]] = defaultdict(list)
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
                candidate.is_installed,
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
