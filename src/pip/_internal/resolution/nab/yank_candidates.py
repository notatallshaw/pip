"""Supply pip's fixed candidate sources to Nab's yanking policy."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from pip._vendor.nab_provider.version_candidates import (
    PreparedVersionCatalogue,
    VersionCandidate,
    VersionCandidateData,
    is_pin,
)
from pip._vendor.nab_resolver.resolver import Resolver
from pip._vendor.nab_resolver.result import IncompleteSelectionError, walk_selected
from pip._vendor.packaging.ranges import VersionRange
from pip._vendor.packaging.utils import canonicalize_name

from pip._internal.exceptions import MetadataInvalid
from pip._internal.resolution.nab.base import Constraint
from pip._internal.resolution.nab.candidates import ExtrasCandidate, as_base_candidate
from pip._internal.resolution.nab.found_candidates import warn_invalid_metadata

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from typing_extensions import override

    from pip._vendor.nab_provider.preference_provider import Package
    from pip._vendor.nab_resolver.resolver import ResolverProvider
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.version import Version

    from .base import Candidate as NativeCandidate
    from .base import Requirement as NativeRequirement
    from .catalogue import CatalogueProvider, DependencyRecord

else:
    MethodType = TypeVar("MethodType")

    def override(method: MethodType) -> MethodType:
        """Keep override annotations free of a runtime typing dependency."""
        return method


def split_extra(package: str) -> tuple[str, str | None]:
    """Split pip's base and extras identifiers for version equality."""
    base, bracket, extras = package.partition("[")
    return base, extras.removesuffix("]") if bracket else None


class Candidate(VersionCandidate):
    """Distinguish version text and withdrawal state at one numeric version."""

    __slots__ = ()

    @property
    @override
    def base(self) -> str:
        return split_extra(self.package)[0]


CandidateData = VersionCandidateData


class YankCandidates(PreparedVersionCatalogue[Candidate, CandidateData]):
    """Cache pip metadata by version text and withdrawal state."""

    range_type = VersionRange

    @staticmethod
    @override
    def pin_project(pin: Requirement) -> str:
        """Normalize the project named by an original pin."""
        return canonicalize_name(pin.name)

    @staticmethod
    @override
    def base_name(package: str) -> str:
        """Remove an extras proxy from a package identifier."""
        return split_extra(package)[0]

    def __init__(self, provider: CatalogueProvider) -> None:
        super().__init__(
            unavailable_reason="a complete preference proof is unavailable"
        )
        self.provider = provider
        self.prepared: dict[Candidate, NativeCandidate] = {}
        self.dependencies: dict[Candidate, DependencyRecord] = {}
        self.rejections: list[str] = []
        self.rejected_versions: set[tuple[str, Version, bool]] = set()

    @override
    def read_choices(self, package: str) -> tuple[Candidate, ...]:
        """List fixed, installed or finder choices without reading index metadata."""
        provider = self.provider
        available: list[Candidate]
        if package.startswith("<"):
            available = [
                Candidate(name, version, withdrawn=False)
                for name, version in provider.candidates
                if name == package
            ]
        else:
            explicit = provider.explicit_candidate(package)
            if explicit is not None:
                available = [Candidate(package, explicit.version, withdrawn=False)]
            elif split_extra(package)[0] in provider.unavailable_sources:
                available = []
            else:
                available = [
                    Candidate(package, item.version, withdrawn=item.link.is_yanked)
                    for item in provider.catalogue(package).candidates
                ]
                installed = provider.installed_version(package)
                if installed is not None:
                    available.insert(0, Candidate(package, installed, withdrawn=False))
        return tuple(available)

    def ordered(
        self,
        package: str,
        version_range: VersionRange,
        *,
        choices: Sequence[Candidate] | None = None,
    ) -> list[Candidate]:
        """Prefer live artifacts and preserve pip's installation and release policy."""
        available = self.choices(package) if choices is None else choices
        provider = self.provider
        installed = (
            None if package.startswith("<") else provider.installed_version(package)
        )
        preferred = installed is not None and not provider.native.eligible_for_upgrade(
            package
        )
        result = self.release_order(package, version_range, available)
        installation = next(
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
        if installation is not None:
            result = [item for item in result if item != installation]
            position = (
                0
                if preferred
                else next(
                    (
                        index
                        for index, item in enumerate(result)
                        if item.withdrawn or item.version <= installation.version
                    ),
                    len(result),
                )
            )
            result.insert(position, installation)
        return result

    def release_order(
        self,
        package: str,
        version_range: VersionRange,
        available: Sequence[Candidate],
    ) -> list[Candidate]:
        """Order live then withdrawn files, retaining prereleases for backtracking."""
        prereleases = (
            None
            if package.startswith("<")
            else self.provider.factory.catalogue_prerelease_policy(package)
        )
        result: list[Candidate] = []
        for withdrawn in (False, True):
            group = [item for item in available if item.withdrawn == withdrawn]
            while group:
                accepted = list(
                    version_range.filter(
                        group,
                        key=lambda item: item.version,
                        prereleases=prereleases,
                    )
                )
                if not accepted:
                    break
                accepted.sort(key=lambda item: item.version, reverse=True)
                result.extend(accepted)
                exhausted = set(accepted)
                group = [item for item in group if item not in exhausted]
        return result

    @override
    def read_candidate(self, candidate: Candidate) -> CandidateData | None:
        """Read metadata only after the proxy establishes permission for a yank."""
        native = self.prepare_candidate(candidate)
        if native is None:
            return None
        return self.record_metadata(candidate, native)

    def record_metadata(
        self, candidate: Candidate, native: NativeCandidate
    ) -> CandidateData:
        """Cache original declarations from an already prepared native artifact."""
        requirements = tuple(self.provider.native.get_dependencies(native))
        ranges = self.provider.ranges(
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
        data = CandidateData(ranges, tuple(pins))
        self.prepared[candidate] = native
        self.provider.has_conditional_prereleases |= (
            native.version.is_prerelease
            and native.source_link is not None
            and native.project_name not in self.provider.explicit_bases
        )
        self.dependencies[candidate] = requirements, ranges
        self.facts[candidate] = data
        if isinstance(native, ExtrasCandidate):
            underlying = Candidate(
                native.base.name, candidate.version, withdrawn=candidate.withdrawn
            )
            if underlying not in self.facts:
                self.record_metadata(underlying, native.base)
        return data

    def prepare_candidate(self, candidate: Candidate) -> NativeCandidate | None:
        """Choose an artifact with matching version text and withdrawal state."""
        provider = self.provider
        package, version = candidate.package, candidate.version
        if package.startswith("<"):
            return provider.candidates[package, version]
        explicit = provider.explicit_candidate(package)
        if explicit is not None:
            return explicit
        if not candidate.withdrawn and candidate.label == str(
            provider.installed_version(package)
        ):
            return provider.factory.installed_candidate(
                package, provider.preparation_template(package)
            )
        rejection_key = package, version, candidate.withdrawn
        if rejection_key in self.rejected_versions:
            return None
        catalogue = provider.catalogue(package)
        for artifact in catalogue.candidates:
            if (
                artifact.version != version
                or str(artifact.version) != candidate.label
                or artifact.link.is_yanked != candidate.withdrawn
            ):
                continue
            try:
                prepared = catalogue.prepare(artifact)
            except MetadataInvalid as error:
                warn_invalid_metadata(version, error)
                self.rejections.append(str(error))
                self.rejected_versions.add(rejection_key)
                return None
            if prepared is not None:
                return prepared
        return None

    def adopt(self, selected: Mapping[str, Candidate]) -> dict[str, Version]:
        """Replace provisional metadata with the selected artifact identities."""
        provider = self.provider
        for package, candidate in selected.items():
            provider.candidates[package, candidate.version] = self.prepared[candidate]
            provider.dependencies[package, candidate.version] = self.dependencies[
                candidate
            ]
        provider.yanked_selection = {
            package for package, candidate in selected.items() if candidate.withdrawn
        }
        return {name: candidate.version for name, candidate in selected.items()}

    def make_resolver(
        self, provider: ResolverProvider[Package, int], limit: int
    ) -> Resolver[Package, int]:
        """Refresh provisional search only after a native metadata fact is observed."""
        return Resolver(
            provider,
            max_iterations=limit,
            availability_generation=lambda: len(self.facts),
        )

    def metadata_is_admitted(
        self, candidate: Candidate, selected: Mapping[str, Candidate]
    ) -> bool:
        """Check conditional prereleases once every selected artifact is known."""
        provider = self.provider
        if not provider.has_conditional_prereleases or provider.has_yanked_candidates:
            return True

        requirements: dict[str, list[NativeRequirement]] = {}
        for requirement in provider.collected.requirements:
            requirements.setdefault(requirement.name, []).append(requirement)

        reachable: dict[str, Candidate] = {}
        try:
            for name, choice, _ in walk_selected(
                provider.roots,
                selected,
                lambda _name, choice: self.dependencies.get(choice, ((), None))[1],
                depth_first=True,
            ):
                reachable[name] = choice
                for requirement in self.dependencies[choice][0]:
                    requirements.setdefault(requirement.name, []).append(requirement)
        except IncompleteSelectionError:
            return True

        if candidate not in reachable.values():
            return True

        return all(
            provider.prerelease_is_admitted(
                self.prepared[choice],
                requirements,
                provider.collected.constraints.get(choice.base, Constraint.empty()),
            )
            for choice in reachable.values()
        )

    def mandatory_bounds(
        self, prescribed: Mapping[str, Candidate]
    ) -> dict[str, VersionRange]:
        """Collect bounds from roots and their reachable fixed parent identities."""
        bounds = {
            name: self.provider.input_bounds(name) for name in self.provider.roots
        }
        expanded: set[str] = set()
        while True:
            implied: list[tuple[str, VersionRange]] = []
            for package, allowed in tuple(bounds.items()):
                base, extra = split_extra(package)
                if extra is not None:
                    implied.append((base, allowed))
                    implied.append(
                        (package, bounds.get(base, self.provider.input_bounds(base)))
                    )
                candidate = prescribed.get(package)
                if candidate is not None:
                    implied.append((package, VersionRange.singleton(candidate.version)))
                data = self.facts.get(candidate) if candidate is not None else None
                if data is not None and package not in expanded:
                    expanded.add(package)
                    implied.extend(data.dependencies.items())
            changed = False
            for package, required in implied:
                previous = bounds.get(package, self.provider.input_bounds(package))
                narrowed = previous & required
                if package not in bounds or narrowed != previous:
                    bounds[package] = narrowed
                    changed = True
            if not changed:
                return bounds

    def scoped_bounds(
        self, package: str, mandatory: Mapping[str, VersionRange]
    ) -> VersionRange:
        """Intersect input bounds with guaranteed base and extra declarations."""
        return (
            self.provider.input_bounds(package)
            & mandatory.get(package, VersionRange.full())
            & mandatory.get(split_extra(package)[0], VersionRange.full())
        )

    def has_complete_metadata(
        self,
        input_pins: Sequence[Requirement],
        *,
        fixed: Iterable[Candidate] = (),
        live: frozenset[str] = frozenset(),
    ) -> bool:
        """Check every potentially reachable, permitted metadata identity."""
        provider = self.provider
        if (
            provider.native.ignore_dependencies
            or provider.has_unavailable_input_source()
        ):
            return True
        demanded = set(provider.roots)
        pins = list(input_pins)
        prescribed = {candidate.package: candidate for candidate in fixed}
        mandatory = self.mandatory_bounds(prescribed)
        if any(allowed.is_empty for allowed in mandatory.values()):
            return True
        expanded: set[Candidate] = set()
        while True:
            changed = False
            for package in tuple(demanded):
                choices = self.candidates.get(package)
                if choices is None:
                    return False
                allowed = self.scoped_bounds(package, mandatory)
                for candidate in choices:
                    if (
                        candidate in expanded
                        or candidate.version not in allowed
                        or prescribed.get(package, candidate) != candidate
                        or (candidate.withdrawn and candidate.base in live)
                    ):
                        continue
                    if candidate.withdrawn and not any(
                        canonicalize_name(req.name) == candidate.base
                        and req.specifier.contains(candidate.version, prereleases=True)
                        for req in pins
                    ):
                        continue
                    if candidate not in self.facts:
                        self.record_cached_extras(candidate)
                    if candidate not in self.facts:
                        rejection = package, candidate.version, candidate.withdrawn
                        if rejection not in self.rejected_versions:
                            return False
                    expanded.add(candidate)
                    changed = True
                    data = self.facts.get(candidate)
                    if data is None:
                        continue
                    if any(
                        (required & self.scoped_bounds(child, mandatory)).is_empty
                        for child, required in data.dependencies.items()
                    ):
                        continue
                    demanded.update(data.dependencies)
                    pins.extend(data.pins)
            if not changed:
                return True

    def record_cached_extras(self, candidate: Candidate) -> None:
        """Expand live extras using an already prepared base artifact."""
        package, extra = split_extra(candidate.package)
        if extra is None or candidate.withdrawn:
            return
        underlying = Candidate(package, candidate.version, withdrawn=False)
        prepared = self.prepared.get(underlying)
        base = None if prepared is None else as_base_candidate(prepared)
        if base is None:
            return
        native = self.provider.factory.make_extras_candidate(
            base,
            frozenset(extra.split(",")),
            comes_from=self.provider.preparation_template(candidate.package),
        )
        self.record_metadata(candidate, native)
