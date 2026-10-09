"""Static version queries with native candidate preparation and final admission."""

from __future__ import annotations

import copy
import logging
from bisect import bisect_left, bisect_right
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, NoReturn, cast

from pip._vendor.nab_resolver.errors import (
    ResolutionError,
    ResolutionTerminatedError,
)
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

from pip._internal.exceptions import (
    DistributionNotFound,
    MetadataInvalid,
    UnsupportedWheel,
)
from pip._internal.models.candidate import InstallationCandidate
from pip._internal.req.constructors import (
    install_req_extend_extras,
    install_req_from_line,
)
from pip._internal.req.req_install import InstallRequirement
from pip._internal.resolution.nab.base import (
    Candidate,
    CatalogueUnsupported,
    Constraint,
    Requirement,
    RequirementCause,
)
from pip._internal.resolution.nab.candidates import (
    BaseCandidate,
    ExtrasCandidate,
    as_base_candidate,
)
from pip._internal.resolution.nab.errors import (
    catalogue_installation_error,
    yanked_installation_error,
)
from pip._internal.resolution.nab.factory import (
    CandidateCatalogue,
    CollectedRootRequirements,
    Factory,
)
from pip._internal.resolution.nab.found_candidates import warn_invalid_metadata
from pip._internal.resolution.nab.provider import PipProvider
from pip._internal.resolution.nab.reporter import PipDebuggingReporter, PipReporter
from pip._internal.resolution.nab.requirements import SpecifierWithoutExtrasRequirement
from pip._internal.resolution.nab.yank_preference import IncompletePreferenceError
from pip._internal.resolution.nab.yanked_resolution import YankProxyProvider
from pip._internal.utils.packaging import get_requirement

if TYPE_CHECKING:
    DependencyRecord = tuple[tuple[Requirement, ...], dict[str, VersionRange]]

logger = logging.getLogger(__name__)
_REORDER_AFTER_VERSIONS = 8


class _TryYankedResolution(Exception):
    """Continue fixed-catalogue search with conditional yank permission."""


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


def has_version_text_aliases(candidates: Iterable[InstallationCandidate]) -> bool:
    """Find numerically equal catalogue versions with different version text."""
    versions: dict[Version, str] = {}
    for candidate in candidates:
        label = str(candidate.version)
        if versions.setdefault(candidate.version, label) != label:
            return True
    return False


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
        self.token_catalogues: set[str] = set()
        self.installed_versions: dict[str, Version | None] = {}
        self.yank_resolution = False
        self.has_yanked_candidates = False
        self.yanked_selection: set[str] = set()
        self.yank_admission_sources: dict[str, tuple[str, ...]] = {}
        self.candidates: dict[tuple[str, Version], Candidate] = {}
        self.dependencies: dict[tuple[str, Version], DependencyRecord] = {}
        self.templates: dict[str, InstallRequirement] = {}
        self.solution_ranges: Mapping[str, RangeProtocol[Version]] = {}
        self.pending_dependencies: list[Incompatibility[str, Version]] = []
        self.matching_counts: dict[tuple[str, RangeProtocol[Version]], int] = {}
        self.universes: dict[str, list[Version]] = {}
        self.prepared_counts: dict[str, int] = {}
        self.requested_order = False
        self.explicit_bases: dict[str, BaseCandidate] = {}
        self.explicit_candidates: dict[str, Candidate] = {}
        self.source_causes: dict[str, RequirementCause] = {}
        self.unavailable_sources: set[str] = set()
        self.rejected_versions: dict[str, set[Version]] = {}
        self.fixed_options: dict[str, Constraint] = {}
        self.collect_fixed_options()
        self.collect_explicit_sources()
        self.factory.catalogue_sources = self.explicit_bases
        self.roots = self.ranges(collected.requirements, roots=True)
        self.constraints: dict[str, VersionRange] = {}
        for package, constraint in collected.constraints.items():
            self.constraints[package] = constraint.specifier.to_range()

    def collect_fixed_options(self) -> None:
        """Combine input hashes and pins before any catalogue is queried."""
        self.fixed_options = dict(self.collected.constraints)
        for requirement in self.collected.requirements:
            _, ireq = requirement.get_candidate_lookup()
            if ireq is None:
                continue
            name = requirement.project_name
            self.fixed_options[name] = (
                self.fixed_options.get(name, Constraint.empty()) & ireq
            )

    def reject_input(self, package: str) -> NoReturn:
        """Report a contradiction proved solely by mandatory input declarations."""
        causes = [
            RequirementCause(req, None)
            for req in self.collected.requirements
            if req.project_name == package
        ]
        if causes:
            raise self.factory.get_input_conflict_error(
                causes, self.collected.constraints
            )
        raise DistributionNotFound(f"Conflicting fixed sources for {package}")

    def expand_fixed_sources(self) -> None:
        """Prepare the URL closure of mandatory explicit input candidates."""
        queue = deque(self.explicit_candidates.values())
        seen: set[str] = set()
        self.factory.catalogue_expanding_sources = True
        try:
            while queue:
                parent = queue.popleft()
                if parent.name in seen:
                    continue
                seen.add(parent.name)
                requirements = tuple(self.native.get_dependencies(parent))
                for requirement in requirements:
                    candidate, _ = requirement.get_candidate_lookup()
                    if candidate is None or candidate.name.startswith("<"):
                        continue
                    self.register_source(
                        candidate, RequirementCause(requirement, parent)
                    )
                    queue.append(candidate)
                self.remember(parent)
                self.dependencies[parent.name, parent.version] = (
                    requirements,
                    self.ranges(requirements),
                )
        finally:
            self.factory.catalogue_expanding_sources = False

    def collect_explicit_sources(self) -> None:
        """Fix input sources before translating any named requirements."""
        for requirement in self.collected.requirements:
            candidate, _ = requirement.get_candidate_lookup()
            if candidate is None:
                continue
            self.register_source(candidate, RequirementCause(requirement, None))

    def register_source(self, candidate: Candidate, cause: RequirementCause) -> None:
        """Record the requirement that fixes a mandatory candidate's source."""
        base = (
            candidate.base
            if isinstance(candidate, ExtrasCandidate)
            else as_base_candidate(candidate)
        )
        assert base is not None, "An explicit source must have a native base candidate"
        previous = self.explicit_bases.setdefault(base.name, base)
        if previous != base:
            raise self.factory.get_installation_error(
                [self.source_causes[base.name], cause], self.collected.constraints
            )
        self.source_causes.setdefault(base.name, cause)
        self.explicit_candidates[candidate.name] = candidate

    def ranges(
        self, requirements: Iterable[Requirement], *, roots: bool = False
    ) -> dict[str, VersionRange]:
        """Translate declarations into ranges over the fixed candidate sources."""
        ranges: dict[str, VersionRange] = {}
        for requirement in requirements:
            candidate, ireq = requirement.get_candidate_lookup()
            if candidate is not None:
                self.remember(candidate)
                allowed = VersionRange.singleton(candidate.version)
            elif ireq is not None:
                assert ireq.link is None, "A specifier requirement cannot carry a link"
                if not roots or not isinstance(
                    requirement, SpecifierWithoutExtrasRequirement
                ):
                    self.templates.setdefault(requirement.name, ireq)
                    if roots:
                        self.templates.setdefault(requirement.project_name, ireq)
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

    def remember(self, candidate: Candidate) -> None:
        key = candidate.name, candidate.version
        previous = self.candidates.setdefault(key, candidate)
        if previous != candidate:
            raise CatalogueUnsupported("same-version source replacement")

    def catalogue(self, package: str) -> CandidateCatalogue:
        """Return the request's fixed artifact list, loading it on first use."""
        if package not in self.catalogues:
            self.catalogues[package] = self.factory.catalogue_candidates(
                package,
                self.preparation_template(package),
                self.fixed_options.get(
                    package.partition("[")[0], Constraint.empty()
                ).hashes,
            )
            candidates = self.catalogues[package].candidates
            yanked = any(item.link.is_yanked for item in candidates)
            self.has_yanked_candidates |= yanked
            if yanked or has_version_text_aliases(candidates):
                self.token_catalogues.add(package)
        catalogue = self.catalogues[package]
        if not self.yank_resolution and package in self.token_catalogues:
            raise _TryYankedResolution(package)
        return catalogue

    def preparation_template(self, package: str) -> InstallRequirement | None:
        """Combine base provenance with the identifier's extras for preparation."""
        base, bracket, _ = package.partition("[")
        template = self.templates.get(base, self.templates.get(package))
        policy = self.collected.constraints.get(base)
        if (
            template is not None
            and policy is not None
            and not template.hash_options
            and any(policy.hash_options.values())
        ):
            template = copy.copy(template)
            template.hash_options = {
                key: list(values) for key, values in policy.hash_options.items()
            }
        if template is not None and bracket:
            return install_req_extend_extras(template, get_requirement(package).extras)
        return template

    def explicit_candidate(self, package: str) -> Candidate | None:
        """Return the source fixed by an input requirement, including its extras."""
        base_name = package.partition("[")[0]
        if base_name in self.unavailable_sources:
            return None
        constraint = self.collected.constraints.get(base_name)
        if (
            base_name not in self.explicit_bases
            and constraint is not None
            and constraint.links
        ):
            self.load_constraint_source(base_name, constraint)
        if not self.explicit_bases:
            return None
        candidate = self.explicit_candidates.get(package)
        if candidate is not None:
            return candidate
        base, bracket, _ = package.partition("[")
        candidate = self.explicit_bases.get(base)
        if candidate is None or not bracket:
            return candidate
        candidate = self.factory.make_extras_candidate(
            self.explicit_bases[base],
            frozenset(get_requirement(package).extras),
            comes_from=self.preparation_template(package),
        )
        self.explicit_candidates[package] = candidate
        return candidate

    def load_constraint_source(self, package: str, constraint: Constraint) -> None:
        """Prepare a constrained source or record incompatible wheel tags."""
        template = self.preparation_template(package) or install_req_from_line(package)
        candidates = self.factory.candidates_from_constraints(
            package, constraint, template
        )
        try:
            candidate = next(
                (item for item in candidates if constraint.is_satisfied_by(item)),
                None,
            )
        except UnsupportedWheel:
            self.unavailable_sources.add(package)
            return
        if candidate is None:
            self.unavailable_sources.add(package)
            return
        constrained_base = as_base_candidate(candidate)
        assert constrained_base is not None
        self.explicit_bases[package] = constrained_base
        self.explicit_candidates[package] = constrained_base

    def installed_version(self, package: str) -> Version | None:
        """Cache installed availability independently of finder versions."""
        if package not in self.installed_versions:
            self.installed_versions[package] = self.factory.installed_version(package)
        return self.installed_versions[package]

    def installed_version_matches_inputs(self, package: str) -> bool:
        """Check whether fixed input version bounds admit the installation."""
        installed = self.installed_version(package)
        if installed is None:
            return False
        base = package.partition("[")[0]
        allowed = self.roots.get(base, VersionRange.full())
        if package != base:
            allowed &= self.roots.get(package, VersionRange.full())
        allowed &= self.constraints.get(base, VersionRange.full())
        return installed in allowed

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
        explicit = self.explicit_candidate(package)
        if explicit is not None:
            if explicit.version not in version_range:
                return None
            self.remember(explicit)
            if self.precheck_dependencies(package, explicit.version):
                return None
            return explicit.version
        if package.partition("[")[0] in self.unavailable_sources:
            return None
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
            if version in self.rejected_versions.get(package, ()):
                continue
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
                try:
                    candidate = catalogue.prepare(artifact)
                except MetadataInvalid as error:
                    warn_invalid_metadata(version, error)
                    self.rejected_versions.setdefault(package, set()).add(version)
                    continue
                if candidate is None:
                    continue
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
        explicit = self.explicit_candidate(package)
        if explicit is not None:
            return explicit.version in version_range
        if package.partition("[")[0] in self.unavailable_sources:
            return False
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
        explicit = self.explicit_candidate(package)
        if explicit is not None:
            return int(explicit.version in version_range)
        if package.partition("[")[0] in self.unavailable_sources:
            return 0
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
        for package, allowed in self.roots.items():
            constraint = self.constraints.get(package, VersionRange.full())
            if (allowed & constraint).is_empty:
                self.reject_input(package.partition("[")[0])
        self.expand_fixed_sources()
        try:
            try:
                return self._solve_once(reporter)
            except _TryRequestedOrder as error:
                logger.info(
                    "Nab catalogue reordered after repeated preparation of %s",
                    str(error),
                )
                self.requested_order = True
                self.solution_ranges = {}
                self.pending_dependencies.clear()
            return self._solve_once(reporter)
        except _TryYankedResolution:
            self.yank_resolution = True
            self.solution_ranges = {}
            self.pending_dependencies.clear()
            return self.solve_yanked(reporter)

    def solve_yanked(
        self, reporter: PipReporter | PipDebuggingReporter
    ) -> Solution[str, Version]:
        """Resolve artifact groups with grounded yank permission."""
        declarations = []
        for requirement in self.collected.requirements:
            _, ireq = requirement.get_candidate_lookup()
            if ireq is not None and ireq.req is not None:
                declarations.append(ireq.req)
        declarations.extend(
            get_requirement(name + str(constraint.specifier))
            for name, constraint in self.collected.constraints.items()
        )
        proxy = YankProxyProvider(self, declarations)
        try:
            pins, _ = proxy.resolve()
        except ResolutionTerminatedError:
            raise
        except IncompletePreferenceError:
            raise
        except ResolutionError as error:
            raise yanked_installation_error(error, proxy) from error
        edges = tuple(
            (name, child)
            for name, version in pins.items()
            for child in self.dependencies[name, version][1]
        )
        for name, version in pins.items():
            reporter.pinning(self.candidates[name, version])
        return Solution(pins, edges, tuple(self.roots))

    def _solve_once(
        self, reporter: PipReporter | PipDebuggingReporter
    ) -> Solution[str, Version]:
        """Run a fresh solver over the retained fixed-catalogue metadata."""
        try:
            return Resolver(
                self,
                range_type=VersionRange,
                root_version=Version("0"),
                observer=CatalogueObserver(self, reporter),
            ).solve(self.roots, self.constraints)
        except ResolutionTerminatedError:
            raise
        except ResolutionError as error:
            if error.incompatibility is not None and self.has_complete_metadata():
                logger.info("Nab catalogue certified failure")
                raise catalogue_installation_error(error, self) from error
            raise

    def has_unavailable_input_source(self) -> bool:
        """Check mandatory fixed sources against the original input version bounds."""
        for package, allowed in self.roots.items():
            base = package.partition("[")[0]
            if base in self.unavailable_sources:
                return True
            candidate = self.explicit_bases.get(base)
            if candidate is None:
                continue
            allowed &= self.constraints.get(base, VersionRange.full())
            if candidate.version not in allowed:
                return True
        return False

    def has_complete_metadata(self) -> bool:
        """Check metadata coverage for the fixed domain used by this attempt."""
        if self.native.ignore_dependencies or self.has_unavailable_input_source():
            return True
        for package, installed in self.installed_versions.items():
            if (
                self.installed_version_matches_inputs(package)
                and (package, installed) not in self.dependencies
            ):
                return False
        referenced = set(self.roots)
        for _, dependencies in self.dependencies.values():
            referenced.update(dependencies)
        covered = (
            set(self.catalogues)
            | set(self.explicit_candidates)
            | self.unavailable_sources
        )
        if any(
            not package.startswith("<") and package not in covered
            for package in referenced
        ):
            return False
        for package, catalogue in self.catalogues.items():
            allowed = self.roots.get(
                package, VersionRange.full()
            ) & self.constraints.get(package, VersionRange.full())
            for artifact in catalogue.candidates:
                # Versions excluded by fixed inputs cannot introduce a dependency.
                if artifact.version not in allowed:
                    continue
                if (package, artifact.version) in self.dependencies:
                    candidate = self.candidates[package, artifact.version]
                    if str(candidate.version) != str(artifact.version):
                        return False
                    continue
                if artifact.version not in self.rejected_versions.get(package, ()):
                    return False
        return all(
            (candidate.name, candidate.version) in self.dependencies
            for candidate in self.explicit_candidates.values()
        )

    def selected_requirements(
        self, solution: Solution[str, Version]
    ) -> dict[str, list[Requirement]]:
        """Collect input declarations and dependencies of the selected candidates."""
        requirements: dict[str, list[Requirement]] = defaultdict(list)
        for requirement in self.collected.requirements:
            requirements[requirement.name].append(requirement)
        for name, version in solution.pins.items():
            for requirement in self.dependencies[name, version][0]:
                requirements[requirement.name].append(requirement)
        return requirements

    def validate(self, solution: Solution[str, Version]) -> bool:
        """Recheck native requirements and artifact order for the selected versions."""
        requirements = self.selected_requirements(solution)
        for name, version in solution.pins.items():
            candidate = self.candidates[name, version]
            if not all(
                requirement.is_satisfied_by(candidate)
                for requirement in requirements[name]
            ):
                logger.info("Nab catalogue admission: %s violates a requirement", name)
                return False
            base = self.collected.constraints.get(
                candidate.project_name, Constraint.empty()
            )
            if not base.is_satisfied_by(candidate):
                logger.info("Nab catalogue admission: %s violates a constraint", name)
                return False
            if (
                candidate.version.is_prerelease
                and (not self.yank_resolution or not self.has_yanked_candidates)
                and candidate.project_name not in self.explicit_bases
                and not self.factory.catalogue_prerelease_admitted(
                    candidate, requirements, base
                )
            ):
                logger.info(
                    "Nab catalogue admission: %s has an ineligible prerelease", name
                )
                return False
            pinned = Constraint(
                base.specifier & SpecifierSet(f"=={version}"),
                base.hashes,
                base.hash_options,
                base.links,
            )
            if (
                self.yank_resolution
                and candidate.project_name not in self.explicit_bases
            ):
                if not self.validate_yanked_artifact(name, candidate):
                    return False
                continue
            choices = self.factory.find_candidates(
                name,
                requirements,
                pinned,
                candidate.is_installed,
                lambda requirement, candidate: requirement.is_satisfied_by(candidate),
            )
            if next(iter(choices), None) != candidate:
                logger.info(
                    "Nab catalogue admission: %s has a different preferred artifact",
                    name,
                )
                return False
        return True

    def validate_yanked_artifact(self, package: str, candidate: Candidate) -> bool:
        """Check finder order within the solver's permitted withdrawal group."""
        if candidate.is_installed or package.startswith("<"):
            return True
        withdrawn = package in self.yanked_selection
        catalogue = self.catalogue(package)
        for artifact in catalogue.candidates:
            if (
                artifact.version != candidate.version
                or str(artifact.version) != str(candidate.version)
                or artifact.link.is_yanked != withdrawn
            ):
                continue
            try:
                prepared = catalogue.prepare(artifact)
            except MetadataInvalid:
                return False
            if prepared is not None:
                return prepared == candidate
        return False

    def selected(self, solution: Solution[str, Version]) -> Mapping[str, Candidate]:
        return {
            name: self.candidates[name, version]
            for name, version in solution.pins.items()
        }
