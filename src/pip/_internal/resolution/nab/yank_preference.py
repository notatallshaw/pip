"""Attach the shared preference search to this provider's candidate catalogue."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pip._vendor.nab_provider.preference import (
    IncompletePreferenceError,
    PreferenceSearch,
    PreparationStatus,
)
from pip._vendor.nab_provider.preference import (
    PreferenceScope as CoreScope,
)
from pip._vendor.nab_resolver.resolver import ResolverStats
from pip._vendor.packaging.ranges import VersionRange
from pip._vendor.packaging.requirements import Requirement

from .yank_candidates import Candidate, override

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pip._vendor.packaging.version import Version

    from .catalogue import CatalogueProvider
    from .yank_candidates import YankCandidates

__all__ = [
    "IncompletePreferenceError",
    "PreferenceScope",
    "PreparationStatus",
    "YankPreference",
]

PreferenceScope = CoreScope[Candidate]


class YankPreference(PreferenceSearch[Candidate, VersionRange, Requirement]):
    """Retain native adoption and diagnostic statistics around the shared search."""

    def __init__(
        self,
        catalogue: YankCandidates,
        roots: Mapping[str, VersionRange],
        input_pins: Sequence[Requirement],
        constraints: Mapping[str, VersionRange],
        *,
        max_steps: int = 100_000,
    ) -> None:
        """Retain native adoption and the provider-specific incomplete-query policy."""
        super().__init__(
            roots,
            input_pins,
            constraints,
            max_steps=max_steps,
            refresh_incomplete=True,
        )

        self._catalogue = catalogue
        self.stats: ResolverStats[Any] = ResolverStats()

    @property
    @override
    def catalogue(self) -> YankCandidates:
        """Return the native catalogue used for preparation and adoption."""
        return self._catalogue

    @override
    def record_solver_stats(self, stats: ResolverStats[Any]) -> None:
        """Accumulate every scalar and package counter before a query is discarded."""
        for name in ResolverStats.__slots__:
            value = getattr(stats, name)
            if isinstance(value, int):
                setattr(self.stats, name, getattr(self.stats, name) + value)
            else:
                target = getattr(self.stats, name)
                for package, count in value.items():
                    target[package] += count

    def adopt(
        self, selected: Mapping[str, Candidate]
    ) -> tuple[dict[str, Version], CatalogueProvider]:
        """Attach the allowing input or dependency pins to the selected provider."""
        sources = self.explain(selected)
        pins, provider = self.catalogue.adopt(selected)
        provider.yank_admission_sources = sources
        return pins, provider
