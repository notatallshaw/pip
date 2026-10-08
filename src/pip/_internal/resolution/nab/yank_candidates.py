"""Supply pip's fixed candidate sources to Nab's yanking policy."""

from __future__ import annotations

import typing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pip._vendor.packaging.ranges import VersionRange

from pip._internal.exceptions import MetadataInvalid
from pip._internal.resolution.nab.base import CatalogueUnsupported
from pip._internal.resolution.nab.candidates import ExtrasCandidate
from pip._internal.resolution.nab.found_candidates import warn_invalid_metadata

if TYPE_CHECKING:
    import sys
    from collections.abc import Mapping, Sequence

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
    """Distinguish live and withdrawn files at the same package version."""

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
    """Cache pip metadata separately for live and withdrawn artifact choices."""

    def __init__(self, provider: CatalogueProvider) -> None:
        self.provider = provider
        self.preferences: dict[str, Version] = {}
        self.candidates: dict[str, tuple[Candidate, ...]] = {}
        self.facts: dict[Candidate, CandidateData | None] = {}
        self.prepared: dict[Candidate, NativeCandidate] = {}
        self.dependencies: dict[Candidate, DependencyRecord] = {}
        self.unavailable: set[Candidate] = set()
        self.missing_listings: set[str] = set()
        self.unavailable_reason = "required metadata is unavailable"
        self.rejections: list[str] = []

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
        requirements = tuple(self.provider.native.get_dependencies(native))
        if native.is_installed and any(
            requirement.name == candidate.package
            and not requirement.is_satisfied_by(native)
            for requirement in requirements
        ):
            reason = "installed self replacement"
            raise CatalogueUnsupported(reason)
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
        self.dependencies[candidate] = requirements, ranges
        self.facts[candidate] = data
        return data

    def prepare_candidate(self, candidate: Candidate) -> NativeCandidate | None:
        """Try compatible artifacts in finder order within one withdrawal group."""
        provider = self.provider
        package, version = candidate.package, candidate.version
        if package.startswith("<"):
            return provider.candidates[package, version]
        explicit = provider.explicit_candidate(package)
        if explicit is not None:
            return explicit
        if not candidate.withdrawn and version == provider.installed_version(package):
            return provider.factory.installed_candidate(
                package, provider.preparation_template(package)
            )
        catalogue = provider.catalogue(package)
        for artifact in catalogue.candidates:
            if (
                artifact.version != version
                or artifact.link.is_yanked != candidate.withdrawn
            ):
                continue
            try:
                prepared = catalogue.prepare(artifact)
            except MetadataInvalid as error:
                if provider.installed_version_matches_inputs(package):
                    raise
                warn_invalid_metadata(version, error)
                self.rejections.append(str(error))
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
