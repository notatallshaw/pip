"""Static version queries with native candidate preparation and final admission."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping

from pip._vendor.nab_resolver.resolver import BaseProvider, Resolver, Solution
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
        self.roots = self.ranges(collected.requirements, roots=True)
        self.constraints = {}
        for package, constraint in collected.constraints.items():
            if constraint.links or constraint.hashes:
                raise CatalogueUnsupported("source or hash constraint")
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
        if package.startswith("<"):
            return next(
                (
                    version
                    for (name, version) in self.candidates
                    if name == package and version in version_range
                ),
                None,
            )
        for version, prepare in self.catalogue(package):
            if version not in version_range:
                continue
            key = package, version
            if key not in self.candidates:
                candidate = prepare()
                if candidate is None:
                    raise CatalogueUnsupported("artifact preparation rejected")
                self.remember(candidate)
            return version
        return None

    def has_satisfying_version(self, package, version_range):
        if package.startswith("<"):
            return any(
                name == package and version in version_range
                for name, version in self.candidates
            )
        return any(version in version_range for version, _ in self.catalogue(package))

    def get_dependencies(self, package, version):
        key = package, version
        if key not in self.dependencies:
            candidate = self.candidates[key]
            requirements = tuple(self.native.get_dependencies(candidate))
            ranges = self.ranges(requirements)
            self.dependencies[key] = requirements, ranges
        return self.dependencies[key][1]

    def prioritize(self, package, version_range, conflict_counts, culprit_counts=None):
        return (
            -conflict_counts.get(package, 0),
            self.collected.user_requested.get(package, float("inf")),
            package,
        )

    def widen_decision(self, package, version):
        return None

    def solve(self) -> Solution[str, Version]:
        return Resolver(self, range_type=VersionRange, root_version=Version("0")).solve(
            self.roots, self.constraints
        )

    def validate(self, solution: Solution[str, Version]) -> bool:
        """Recheck final native requirements and artifact order within each selected version."""
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
            # Catalogue candidates exclude yanks and prereleases before version pinning.
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
