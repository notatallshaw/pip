"""Evaluate a PEP 508 dependency marker for a resolve-time environment.

Its own module because it is the resolve path's only marker-set
dependency.  ``packaging.markers.Marker.evaluate`` binds ``extra`` to a
single string, which cannot say "these three extras are active", so this
goes through :class:`~packaging.markersets.MarkerSet` instead.  Everything
that needs the predicate takes it as an argument, so the engine never
imports the marker-set engine to get it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._conflict_kind import EMPTY_MEMBERSHIP_SETS
from pip._vendor.packaging.markersets import MarkerSet

if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Set as AbstractSet

    from pip._vendor.packaging.markers import Marker


def dependency_marker_holds(
    marker: Marker, environment: Mapping[str, str | AbstractSet[str]]
) -> bool:
    """Evaluate a dependency marker for a resolve-time ``environment``.

    ``extra`` is set-valued: bound to the active extra names, ``extra == "x"``
    tests membership and ``extra != "x"`` non-membership, both PEP 685
    normalised.  It defaults to the empty set when ``environment`` omits it.

    A standard variable a marker names but ``environment`` omits raises
    ``UndefinedEnvironmentName``; callers pass a complete
    ``ResolveTarget.marker_env``.  The lockfile-only set variables are seeded
    empty, so a marker that tests one evaluates to False rather than raising.
    """
    env: dict[str, str | AbstractSet[str]] = {"extra": frozenset()}
    env.update(environment)
    env.update(EMPTY_MEMBERSHIP_SETS)

    return MarkerSet.from_marker(marker).evaluate(env)
