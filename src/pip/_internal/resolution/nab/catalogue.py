"""Bind pip's fixed artifact sources to Nab's prepared catalogue."""

from __future__ import annotations

import copy
import logging
import typing
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, NoReturn

from pip._vendor.nab_provider.preference import IncompletePreferenceError
from pip._vendor.nab_provider.prepared_resolution import PreparedResolution
from pip._vendor.nab_provider.version_candidates import (
    PreparedVersionCatalogue,
    VersionCandidate,
    VersionCandidateData,
    is_pin,
)
from pip._vendor.nab_resolver.errors import ResolutionError, ResolutionTerminatedError
from pip._vendor.nab_resolver.resolver import Solution
from pip._vendor.packaging.ranges import VersionRange
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import Version

from pip._internal.exceptions import (
    DistributionNotFound,
    MetadataInvalid,
    UnsupportedWheel,
)
from pip._internal.models.candidate import InstallationCandidate
from pip._internal.models.link import Link
from pip._internal.req.constructors import (
    install_req_extend_extras,
    install_req_from_line,
)
from pip._internal.req.req_install import InstallRequirement
from pip._internal.resolution.nab.base import (
    Candidate as NativeCandidate,
)
from pip._internal.resolution.nab.base import (
    Constraint,
    Requirement,
    RequirementCause,
)
from pip._internal.resolution.nab.candidates import (
    ExtrasCandidate,
    as_base_candidate,
)
from pip._internal.resolution.nab.errors import catalogue_installation_error
from pip._internal.resolution.nab.factory import (
    CollectedRootRequirements,
    Factory,
)
from pip._internal.resolution.nab.found_candidates import warn_invalid_metadata
from pip._internal.resolution.nab.reporter import PipDebuggingReporter
from pip._internal.resolution.nab.requirements import (
    RequiresPythonRequirement,
    SpecifierWithoutExtrasRequirement,
)
from pip._internal.utils.packaging import get_requirement

if TYPE_CHECKING:
    from typing_extensions import override

    from pip._vendor.packaging.requirements import Requirement as PinDeclaration

else:
    override = getattr(typing, "override", lambda method: method)

logger = logging.getLogger(__name__)


def split_extra(package: str) -> tuple[str, str | None]:
    """Split pip's base and extras identifiers."""
    base, bracket, extras = package.partition("[")
    return base, extras.removesuffix("]") if bracket else None


class Candidate(VersionCandidate):
    """Keep concrete finder files distinct at the same version."""

    __slots__ = ("artifact", "origin", "source_key")
    __record_fields__ = (*VersionCandidate.__slots__, "artifact", "source_key")
    __record_init_fields__ = (
        "package",
        "version",
        "withdrawn",
        "artifact",
        "origin",
        "source_key",
    )

    artifact: InstallationCandidate | None
    origin: NativeCandidate | None
    source_key: tuple[Link, bool, bool] | None

    def __init__(
        self,
        package: str,
        version: Version,
        *,
        withdrawn: bool,
        artifact: InstallationCandidate | None = None,
        origin: NativeCandidate | None = None,
        source_key: tuple[Link, bool, bool] | None = None,
    ) -> None:
        super().__init__(package, version, withdrawn=withdrawn)
        self.artifact = artifact
        self.origin = origin
        self.source_key = source_key

    @property
    @override
    def base(self) -> str:
        return split_extra(self.package)[0]


class CandidateData(VersionCandidateData):
    """Cache native candidates and their declarations."""

    __slots__ = ("native", "requirements")
    __match_args__ = ("native", "requirements", "dependencies", "pins")

    native: NativeCandidate
    requirements: tuple[Requirement, ...]

    def __init__(
        self,
        native: NativeCandidate,
        requirements: tuple[Requirement, ...],
        dependencies: dict[str, VersionRange],
        pins: tuple[PinDeclaration, ...],
    ) -> None:
        super().__init__(dependencies, pins)
        self.native = native
        self.requirements = requirements

    @property
    def original_requirements(self) -> tuple[Requirement, ...]:
        if isinstance(self.native, ExtrasCandidate):
            # Extras emit one synthetic base equality before their metadata.
            assert self.requirements
            assert self.requirements[0].get_candidate_lookup()[0] is self.native.base
            return self.requirements[1:]

        return self.requirements


class PipCatalogue(PreparedVersionCatalogue[Candidate, CandidateData]):
    """Adapt Pip candidates to Nab's prepared solver."""

    range_type = VersionRange

    def __init__(
        self,
        factory: Factory,
        collected: CollectedRootRequirements,
        *,
        ignore_dependencies: bool,
        upgrade_strategy: str,
    ) -> None:
        super().__init__(unavailable_reason="required metadata is unavailable")
        self.factory = factory
        self.ignore_dependencies = ignore_dependencies
        self.upgrade_strategy = upgrade_strategy
        self.collected = collected

        self.native_candidates: dict[tuple[str, Version], NativeCandidate] = {}
        self.installations: dict[
            str, tuple[Version, Callable[[], NativeCandidate]] | None
        ] = {}
        self.selection: Mapping[str, Candidate] = {}
        self.rejections: list[str] = []
        self.invalid_versions: set[tuple[str, Version, bool]] = set()
        self.templates: dict[str, InstallRequirement] = {}
        self.has_yanked_candidates = False

        # Preserve prepared input identities before binding any dependencies.
        for requirement in collected.requirements:
            candidate, _ = requirement.get_candidate_lookup()
            if candidate is not None:
                self.factory.seed_source(candidate)
        self.roots = self.ranges(collected.requirements, roots=True)
        self.constraints = {
            package: constraint.specifier.to_range()
            for package, constraint in collected.constraints.items()
        }

    @staticmethod
    @override
    def pin_project(pin: PinDeclaration) -> str:
        return canonicalize_name(pin.name)

    @staticmethod
    @override
    def base_name(package: str) -> str:
        return split_extra(package)[0]

    @staticmethod
    @override
    def base_candidate(candidate: Candidate) -> Candidate:
        return Candidate(
            candidate.base,
            candidate.version,
            withdrawn=candidate.withdrawn,
            artifact=candidate.artifact,
            origin=candidate.origin,
            source_key=candidate.source_key,
        )

    def eligible_for_upgrade(self, package: str) -> bool:
        if self.upgrade_strategy == "eager":
            return True
        return self.upgrade_strategy == "only-if-needed" and any(
            requested.partition("[")[0] == package.partition("[")[0]
            for requested in self.collected.user_requested
        )

    def get_dependencies(self, candidate: NativeCandidate) -> Iterable[Requirement]:
        return (
            requirement
            for requirement in candidate.iter_dependencies(not self.ignore_dependencies)
            if requirement is not None
        )

    def input_sources(self, package: str) -> list[NativeCandidate] | None:
        """Read sources declared in requirements and constraints."""
        base = split_extra(package)[0]
        explicit = [
            candidate
            for requirement in self.collected.requirements
            if requirement.project_name == base
            and (candidate := requirement.get_candidate_lookup()[0]) is not None
        ]
        constraint = self.collected.constraints.get(base, Constraint.empty())
        if explicit:
            return [
                candidate
                for candidate in explicit
                if constraint.is_satisfied_by(candidate)
            ]
        if not constraint.links:
            return None

        template = self.preparation_template(package) or install_req_from_line(package)
        try:
            candidates = self.factory.candidates_from_constraints(
                package, constraint, template
            )
            sources = [
                candidate
                for candidate in candidates
                if constraint.is_satisfied_by(candidate)
            ]
            for candidate in sources:
                self.factory.seed_source(candidate)
            return sources
        except UnsupportedWheel:
            return []

    def source_candidate(self, package: str, native: NativeCandidate) -> Candidate:
        """Keep a known source optional until its original declaration is active."""
        base = native.base if isinstance(native, ExtrasCandidate) else native
        return Candidate(
            package,
            base.version,
            withdrawn=False,
            origin=base,
            source_key=self.factory.candidate_source_key(base),
        )

    def reject_input(self, package: str) -> NoReturn:
        """Report conflicting inputs supplied by the user."""
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

    def ranges(
        self, requirements: Iterable[Requirement], *, roots: bool = False
    ) -> dict[str, VersionRange]:
        """Project original requirements into version bounds."""
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

    def remember(self, candidate: NativeCandidate) -> None:
        key = candidate.name, candidate.version
        self.native_candidates.setdefault(key, candidate)

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

    def installed_version(self, package: str) -> Version | None:
        """Cache installed availability independently of finder versions."""
        if package not in self.installations:
            self.installations[package] = self.factory.installed_candidate_info(
                package, self.preparation_template(package)
            )
        info = self.installations[package]
        return None if info is None else info[0]

    def input_bounds(self, package: str) -> VersionRange:
        """Intersect immutable base, extras and constraint version declarations."""
        base = package.partition("[")[0]
        allowed = self.roots.get(base, VersionRange.full())
        if package != base:
            allowed &= self.roots.get(package, VersionRange.full())
        return allowed & self.constraints.get(base, VersionRange.full())

    def prerelease_is_admitted(
        self,
        candidate: NativeCandidate,
        requirements: Mapping[str, Iterable[Requirement]],
        constraint: Constraint,
    ) -> bool:
        """Apply original declarations to prereleases outside withdrawn groups."""
        if not candidate.version.is_prerelease or self.has_yanked_candidates:
            return True
        return self.factory.catalogue_prerelease_admitted(
            candidate, requirements, constraint
        )

    @override
    def read_choices(self, package: str) -> tuple[Candidate, ...]:
        """List inputs, installed choices and finder files without index metadata."""
        if package.startswith("<"):
            return tuple(
                Candidate(name, version, withdrawn=False)
                for name, version in self.native_candidates
                if name == package
            )

        inputs = self.input_sources(package)
        if inputs is not None:
            return tuple(
                self.source_candidate(package, candidate) for candidate in inputs
            )

        artifacts = self.factory.index_candidates(package)
        allowed = self.input_bounds(package)
        self.has_yanked_candidates |= any(
            item.link.is_yanked for item in artifacts if item.version in allowed
        )
        available = [
            Candidate(
                package,
                item.version,
                withdrawn=item.link.is_yanked,
                artifact=item,
            )
            for item in artifacts
        ]
        installed = self.installed_version(package)
        if installed is not None:
            available.insert(0, Candidate(package, installed, withdrawn=False))

        discovered = [
            self.source_candidate(package, candidate)
            for candidate in self.factory.observed_sources.get(
                canonicalize_name(split_extra(package)[0]), ()
            )
        ]
        # Keep installed self-replacements ahead of unrelated index upgrades.
        refinements = [
            candidate
            for candidate in discovered
            if candidate.source_key in self.factory.observed_self_sources
            and not self.eligible_for_upgrade(package)
        ]
        return tuple(
            refinements
            + available
            + [candidate for candidate in discovered if candidate not in refinements]
        )

    def ordered(
        self,
        package: str,
        version_range: VersionRange,
        *,
        choices: Sequence[Candidate] | None = None,
    ) -> list[Candidate]:
        """Apply live-file and installed-candidate preferences."""
        available = self.choices(package) if choices is None else choices
        installed = None if package.startswith("<") else self.installed_version(package)
        preferred = installed is not None and not self.eligible_for_upgrade(package)
        result = self.order_candidates(package, version_range, available)
        same_version_choice = next(
            (
                item
                for item in available
                if not item.withdrawn
                and item.version == installed
                and item.label == str(installed)
                and item.version in version_range
            ),
            None,
        )
        if same_version_choice is None:
            return result

        result = [item for item in result if item != same_version_choice]
        if preferred:
            position = 0
        else:
            position = next(
                (
                    index
                    for index, item in enumerate(result)
                    if item.withdrawn or item.version <= same_version_choice.version
                ),
                len(result),
            )
        result.insert(position, same_version_choice)
        return result

    @override
    def candidate_prereleases(self, package: str) -> bool | None:
        return (
            None
            if package.startswith("<")
            else self.factory.catalogue_prerelease_policy(package)
        )

    @override
    def order_candidate_batch(
        self, package: str, candidates: Sequence[Candidate]
    ) -> list[Candidate]:
        # Finder order includes file-format preference, not only version precedence.
        return list(candidates)

    @override
    def read_candidate(self, candidate: Candidate) -> CandidateData | None:
        """Prepare metadata after the provider admits this candidate."""
        underlying = self.base_candidate(candidate)
        if (
            candidate.artifact is not None
            and (candidate.base, candidate.version, candidate.withdrawn)
            in self.invalid_versions
        ):
            return None
        if underlying in self.facts and self.facts[underlying] is None:
            return None

        native = self.prepare_candidate(candidate)
        if native is None:
            return None

        requirements = self.native_requirements(native)
        if requirements is None:
            return None

        return self.record_metadata(candidate, native, requirements)

    def native_requirements(
        self, candidate: NativeCandidate
    ) -> tuple[Requirement, ...] | None:
        """Keep Python conflicts and stop at installed self-replacements."""
        # Check base Python support before reading extra-only dependencies.
        if isinstance(candidate, ExtrasCandidate) and not candidate.base.is_installed:
            python_requirement = self.factory.make_requires_python_requirement(
                candidate.base.dist.requires_python
            )
            if (
                python_requirement is not None
                and python_requirement.get_candidate_lookup()[0] is None
            ):
                return (
                    self.factory.make_requirement_from_candidate(candidate.base),
                    python_requirement,
                )

        requirements: list[Requirement] = []
        for requirement in self.get_dependencies(candidate):
            if (
                isinstance(requirement, RequiresPythonRequirement)
                and requirement.get_candidate_lookup()[0] is None
            ):
                # Keep the Python conflict without reading irrelevant dependencies.
                requirements.append(requirement)
                break

            subject = candidate
            if (
                isinstance(candidate, ExtrasCandidate)
                and requirement.name == candidate.base.name
            ):
                subject = candidate.base
            if (
                subject.is_installed
                and requirement.name == subject.name
                and not requirement.is_satisfied_by(subject)
            ):
                return None
            requirements.append(requirement)
        return tuple(requirements)

    def prepare_candidate(self, candidate: Candidate) -> NativeCandidate | None:
        """Prepare the chosen file or installed distribution."""
        package, version = candidate.package, candidate.version
        if package.startswith("<"):
            return self.native_candidates[package, version]
        if candidate.origin is not None:
            base = as_base_candidate(candidate.origin)
            assert base is not None
            extras = get_requirement(package).extras
            return (
                self.factory.make_extras_candidate(
                    base,
                    frozenset(extras),
                    comes_from=self.preparation_template(package),
                )
                if extras
                else base
            )
        if (
            candidate.artifact is None
            and not candidate.withdrawn
            and candidate.label == str(self.installed_version(package))
        ):
            info = self.installations[package]
            assert info is not None
            return info[1]()
        artifact = candidate.artifact
        assert artifact is not None
        try:
            return self.factory.prepare_index_candidate(
                package, artifact, self.preparation_template(package)
            )
        except MetadataInvalid as error:
            warn_invalid_metadata(version, error)
            self.rejections.append(str(error))
            self.invalid_versions.add(
                (candidate.base, candidate.version, candidate.withdrawn)
            )
            self.facts[self.base_candidate(candidate)] = None
            return None

    def selection_is_admitted(self, selected: Mapping[str, Candidate]) -> bool:
        """Check native requirements, policy and source authority for the plan."""
        requirements: dict[str, list[Requirement]] = {}
        for requirement in self.collected.requirements:
            requirements.setdefault(requirement.name, []).append(requirement)

        # Keep synthetic extras/base identity checks in the complete plan.
        for choice in selected.values():
            for requirement in self.selected_data(choice).requirements:
                requirements.setdefault(requirement.name, []).append(requirement)

        for package, choice in selected.items():
            native = self.selected_data(choice).native
            constraint = self.collected.constraints.get(choice.base, Constraint.empty())
            declarations = tuple(requirements.get(package, ()))
            if not all(
                self.requirement_is_satisfied_by(requirement, native)
                for requirement in declarations
            ):
                return False
            if not constraint.is_satisfied_by(native):
                return False
            if choice.origin is None and not self.prerelease_is_admitted(
                native, requirements, constraint
            ):
                return False
        return self.sources_are_admitted(selected)

    def requirement_is_satisfied_by(
        self, requirement: Requirement, native: NativeCandidate
    ) -> bool:
        if not requirement.is_satisfied_by(native):
            return False
        required, _ = requirement.get_candidate_lookup()
        if required is None or required.source_link is None:
            return True

        # Native link equality does not distinguish index and direct provenance.
        required_source = self.factory.candidate_source_key(required)
        return required_source == self.factory.candidate_source_key(native)

    def sources_are_admitted(self, selected: Mapping[str, Candidate]) -> bool:
        """Check source authority along rooted dependency paths."""
        # Input requirements seed the rooted dependency walk.
        rooted_requirements: dict[str, list[Requirement]] = {}
        for requirement in self.collected.requirements:
            rooted_requirements.setdefault(requirement.name, []).append(requirement)
        demanded_packages = set(rooted_requirements)
        admitted_packages: set[str] = set()
        pending = dict(selected)

        while pending:
            pending_count = len(pending)
            for package, choice in tuple(pending.items()):
                if package not in demanded_packages:
                    continue
                data = self.selected_data(choice)
                rooted_declarations = tuple(rooted_requirements.get(package, ()))
                if package != choice.base:
                    rooted_declarations += tuple(
                        rooted_requirements.get(choice.base, ())
                    )

                # An admitted base authorizes extras of that same artifact.
                selected_base = selected.get(choice.base)
                base_is_admitted = (
                    choice.base in admitted_packages
                    and selected_base == self.base_candidate(choice)
                )
                constraint = self.collected.constraints.get(
                    choice.base, Constraint.empty()
                )
                if (
                    choice.origin is not None
                    and not base_is_admitted
                    and not self.source_is_admitted(
                        choice,
                        data.native,
                        constraint,
                        rooted_declarations=rooted_declarations,
                    )
                ):
                    continue

                admitted_packages.add(package)

                # Synthetic extras equality adds demand, never source authority.
                demanded_packages.update(
                    requirement.name for requirement in data.requirements
                )
                for requirement in data.original_requirements:
                    rooted_requirements.setdefault(requirement.name, []).append(
                        requirement
                    )
                del pending[package]

            if len(pending) == pending_count:
                return False

        return True

    def source_is_admitted(
        self,
        choice: Candidate,
        native: NativeCandidate,
        constraint: Constraint,
        *,
        rooted_declarations: tuple[Requirement, ...],
    ) -> bool:
        """Authorize sources from rooted declarations or installed self-replacement."""
        if choice.source_key is None:
            return True
        if constraint.links and constraint.is_satisfied_by(native):
            return True
        for requirement in rooted_declarations:
            subject = (
                native.base
                if isinstance(native, ExtrasCandidate)
                and requirement.name == native.base.name
                else native
            )
            required, _ = requirement.get_candidate_lookup()
            if required is not None and self.requirement_is_satisfied_by(
                requirement, subject
            ):
                return True

        if (
            choice.source_key not in self.factory.observed_self_sources
            or not rooted_declarations
        ):
            return False

        # The replacement's own metadata cannot disqualify its installed subject.
        info = self.factory.installed_candidate_info(
            choice.base, self.preparation_template(choice.base)
        )
        if info is None:
            return False
        installed_base = as_base_candidate(info[1]())
        assert installed_base is not None
        installed = (
            self.factory.make_extras_candidate(installed_base, native.extras)
            if isinstance(native, ExtrasCandidate)
            else installed_base
        )
        if not constraint.is_satisfied_by(installed_base):
            return False
        for requirement in rooted_declarations:
            subject = (
                installed_base if requirement.name == installed_base.name else installed
            )
            if not self.requirement_is_satisfied_by(requirement, subject):
                return False

        return True

    def record_metadata(
        self,
        candidate: Candidate,
        native: NativeCandidate,
        requirements: tuple[Requirement, ...],
    ) -> CandidateData:
        """Retain one prepared identity and its original dependency declarations."""
        ranges = self.ranges(
            requirement
            for requirement in requirements
            if not isinstance(native, ExtrasCandidate)
            or requirement.get_candidate_lookup()[0] is not native.base
        )
        if isinstance(native, ExtrasCandidate):
            ranges[native.base.name] = VersionRange.singleton(native.version)
        pins = []
        for requirement in requirements:
            _, ireq = requirement.get_candidate_lookup()
            if ireq is not None and ireq.req is not None and is_pin(ireq.req):
                pins.append(ireq.req)
        data = CandidateData(native, requirements, ranges, tuple(pins))
        self.facts[candidate] = data

        if isinstance(native, ExtrasCandidate):
            underlying = self.base_candidate(candidate)
            if underlying not in self.facts:
                base_requirements = self.native_requirements(native.base)
                if base_requirements is None:
                    self.facts[underlying] = None
                else:
                    self.record_metadata(underlying, native.base, base_requirements)
        return data

    def selected_data(self, candidate: Candidate) -> CandidateData:
        data = self.facts[candidate]
        assert data is not None
        return data

    def exact_dependencies(self, candidate: Candidate) -> tuple[Candidate, ...]:
        """Map cached native bindings to exact candidate identities."""
        data = self.selected_data(candidate)
        dependencies = []
        for requirement in data.requirements:
            required, _ = requirement.get_candidate_lookup()
            if required is None:
                continue

            # Self/base edges retain finder identity, not merely native equality.
            if required is data.native:
                dependencies.append(candidate)
            elif (
                isinstance(data.native, ExtrasCandidate)
                and required is data.native.base
            ):
                dependencies.append(self.base_candidate(candidate))
            elif required.name.startswith("<"):
                dependencies.append(
                    Candidate(required.name, required.version, withdrawn=False)
                )
            else:
                dependencies.append(self.source_candidate(required.name, required))
        return tuple(dependencies)

    def preferred_candidate(self, package: str) -> Candidate | None:
        """Return a cached input or installed choice without index lookup."""
        if package.startswith("<"):
            return next(
                (
                    Candidate(name, version, withdrawn=False)
                    for name, version in self.native_candidates
                    if name == package
                ),
                None,
            )
        base = split_extra(package)[0]
        for requirement in self.collected.requirements:
            candidate, _ = requirement.get_candidate_lookup()
            if candidate is not None and requirement.project_name == base:
                return self.source_candidate(package, candidate)
        constraint = self.collected.constraints.get(base)
        if constraint is not None and constraint.links:
            return None

        installed = self.installed_version(package)
        if installed is None or self.eligible_for_upgrade(package):
            return None
        return Candidate(package, installed, withdrawn=False)

    def solve(
        self,
        reporter: PipDebuggingReporter | None = None,
        *,
        max_rounds: int | None = None,
        on_candidate_considered: Callable[[Candidate], None] | None = None,
    ) -> Solution[str, Version]:
        if reporter is not None:
            reporter.starting()

        for package, allowed in self.roots.items():
            if (allowed & self.constraints.get(package, VersionRange.full())).is_empty:
                self.reject_input(package.partition("[")[0])

        declarations = []
        for requirement in self.collected.requirements:
            _, ireq = requirement.get_candidate_lookup()
            if ireq is not None and ireq.req is not None:
                declarations.append(ireq.req)
        declarations.extend(
            get_requirement(name + str(constraint.specifier))
            for name, constraint in self.collected.constraints.items()
        )

        session = PreparedResolution.from_catalogue(
            self,
            self.roots,
            tuple(req for req in declarations if is_pin(req)),
            self.constraints,
            refresh_incomplete=True,
            selection_policy=self.selection_is_admitted,
            preferred_candidate=self.preferred_candidate,
            max_rounds=max_rounds,
            on_candidate_considered=on_candidate_considered,
            exact_dependencies=self.exact_dependencies,
        )
        try:
            result = session.solve()
            selected = result.candidates
        except (ResolutionTerminatedError, IncompletePreferenceError):
            raise
        except ResolutionError as error:
            raise catalogue_installation_error(error, session.provider, self) from error
        finally:
            self.remaining_rounds = session.remaining_rounds

        self.selection = selected
        pins = {name: candidate.version for name, candidate in selected.items()}
        if reporter is not None:
            for candidate in selected.values():
                reporter.pinning(self.selected_data(candidate).native)
        return Solution(pins, result.edges, result.roots)

    def selected_requirements(
        self, solution: Solution[str, Version]
    ) -> dict[str, list[Requirement]]:
        requirements: dict[str, list[Requirement]] = defaultdict(list)
        for requirement in self.collected.requirements:
            requirements[requirement.name].append(requirement)
        for package in solution.pins:
            for requirement in self.selected_data(self.selection[package]).requirements:
                requirements[requirement.name].append(requirement)
        return requirements

    def selected(
        self, solution: Solution[str, Version]
    ) -> Mapping[str, NativeCandidate]:
        return {
            name: self.selected_data(self.selection[name]).native
            for name in solution.pins
        }

    def selected_index_reinstalls(self) -> frozenset[str]:
        """Identify selected index files replacing the same installed version."""
        return frozenset(
            choice.base
            for choice in self.selection.values()
            if choice.artifact is not None
            and self.installed_version(choice.package) == choice.version
        )
