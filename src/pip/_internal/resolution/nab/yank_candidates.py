"""Supply pip's fixed candidate sources to Nab's yanking policy."""

from __future__ import annotations

import typing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pip._vendor.packaging.ranges import VersionRange
from pip._vendor.packaging.utils import canonicalize_name

from pip._internal.exceptions import MetadataInvalid
from pip._internal.resolution.nab.candidates import ExtrasCandidate, as_base_candidate
from pip._internal.resolution.nab.found_candidates import warn_invalid_metadata

if TYPE_CHECKING:
    import sys
    from collections.abc import Iterable, Mapping, Sequence

    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.version import Version

    if sys.version_info >= (3, 12):
        from typing import override
    else:
        from typing_extensions import override

    from .base import Candidate as NativeCandidate
    from .catalogue import CatalogueProvider, DependencyRecord

else:
    override = getattr(typing, "override", lambda method: method)


def split_extra(package: str) -> tuple[str, str | None]:
    """Split pip's base and extras identifiers for version equality."""
    base, bracket, extras = package.partition("[")
    return base, extras.removesuffix("]") if bracket else None


@dataclass(frozen=True, slots=True)
class Candidate:
    """Distinguish version text and withdrawal state at one numeric version."""

    package: str
    version: Version
    withdrawn: bool
    label: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", str(self.version))

    @property
    def base(self) -> str:
        return split_extra(self.package)[0]


@dataclass(slots=True)
class CandidateData:
    """Retain active dependency ranges and their original exact-pin syntax."""

    dependencies: dict[str, VersionRange]
    pins: tuple[Requirement, ...]


class UnavailableMetadataError(Exception):
    """Unknown metadata cannot establish that a live alternative fails."""


def is_pin(requirement: Requirement) -> bool:
    return any(
        item.operator == "==="
        or (item.operator == "==" and not item.version.endswith(".*"))
        for item in requirement.specifier
    )


class YankCandidates:
    """Cache pip metadata by version text and withdrawal state."""

    def __init__(self, provider: CatalogueProvider) -> None:
        self.provider = provider
        self.preferences: dict[str, Version] = {}
        self.candidates: dict[str, tuple[Candidate, ...]] = {}
        self.facts: dict[Candidate, CandidateData | None] = {}
        self.prepared: dict[Candidate, NativeCandidate] = {}
        self.dependencies: dict[Candidate, DependencyRecord] = {}
        self.unavailable: set[Candidate] = set()
        self.missing_listings: set[str] = set()
        self.unavailable_reason = "a complete preference proof is unavailable"
        self.rejections: list[str] = []
        self.rejected_versions: set[tuple[str, Version, bool]] = set()

    def choices(self, package: str) -> tuple[Candidate, ...]:
        """List fixed, installed or finder choices without reading index metadata."""
        if package in self.candidates:
            return self.candidates[package]
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
                available = list(
                    dict.fromkeys(
                        Candidate(package, item.version, item.link.is_yanked)
                        for item in provider.catalogue(package).candidates
                    )
                )
                installed = provider.installed_version(package)
                if installed is not None:
                    available.insert(0, Candidate(package, installed, withdrawn=False))
        result = tuple(dict.fromkeys(available))
        self.candidates[package] = result
        return result

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

    def prepare(self, candidate: Candidate) -> CandidateData | None:
        """Read metadata only after the proxy establishes permission for a yank."""
        if candidate in self.facts:
            return self.facts[candidate]
        native = self.prepare_candidate(candidate)
        if native is None:
            self.facts[candidate] = None
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

    def adopt(
        self, selected: Mapping[str, Candidate]
    ) -> tuple[dict[str, Version], CatalogueProvider]:
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
        return {
            name: candidate.version for name, candidate in selected.items()
        }, provider

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
        expanded: set[Candidate] = set()
        while True:
            changed = False
            for package in tuple(demanded):
                choices = self.candidates.get(package)
                if choices is None:
                    return False
                allowed = provider.input_bounds(package)
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
                    if data is not None:
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
