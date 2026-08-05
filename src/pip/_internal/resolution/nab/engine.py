"""The seam between the pip adapter and the nab engine.

Everything on the pip side of this module is written and tested. Everything
below :func:`solve` needs nab to be vendored, so :func:`solve` raises
``NotImplementedError`` naming exactly what is missing rather than returning
something plausible.

The engine's answer is described here in pip's own terms, so the adapter can
be built and tested against it before nab exists, and so nab's result types
stop at this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pip._internal.resolution.nab.inputs import is_pinned

if TYPE_CHECKING:
    from pip._vendor.packaging.specifiers import SpecifierSet
    from pip._vendor.packaging.utils import NormalizedName
    from pip._vendor.packaging.version import Version

    from pip._internal.resolution.nab.candidates import PipHostIndex
    from pip._internal.resolution.nab.inputs import ResolveInputs
    from pip._internal.resolution.nab.observer import NabReporter


ENGINE_MISSING = (
    "The nab resolver cannot run yet: nab is not vendored into pip. The pip "
    "side of the adapter is complete (candidate universe, metadata, "
    "installation order, RequirementSet construction), and what is missing "
    "is the engine itself plus four things only nab can provide: PEP 508 "
    "requirement strings parsed into version ranges, the PubGrub search over "
    "those ranges, extras modelled as proxy packages, and the derivation "
    "tree a failure is explained from."
)


@dataclass(frozen=True)
class ResolvedPin:
    """One package the engine decided on.

    ``key`` is the engine's key, which carries the ``[extras]`` part for an
    extras node. pip needs the split because extras collapse back onto the
    base requirement.
    """

    key: str
    project_name: NormalizedName
    extras: frozenset[NormalizedName]
    version: Version


@dataclass(frozen=True)
class Solution:
    """The engine's answer, in the shape pip consumes it.

    ``edges`` are ``(parent_key, child_key)`` pairs with ``None`` for a
    requirement the user asked for directly. That is the graph
    ``get_topological_weights`` walks, and the ``None`` root is not optional:
    the weighting starts from it.
    """

    pins: tuple[ResolvedPin, ...]
    edges: tuple[tuple[str | None, str], ...]
    roots: tuple[str, ...]


class YankPolicy:
    """PEP 592, applied where the merged requirement is known.

    pip's rule is that a yanked version is used only when every candidate
    that satisfies the merged requirement is yanked and that requirement pins
    a single version. Both halves are decided during the search, not before
    it: the set of satisfying candidates depends on the range in play, and
    the merged requirement includes transitive requirements the adapter never
    sees.

    So the policy is passed into the engine rather than applied to the
    universe. The engine calls it once it knows both halves.
    """

    def __init__(self, pinned_on_command_line: frozenset[NormalizedName]) -> None:
        self._pinned = pinned_on_command_line

    def admits_yanked(
        self,
        project_name: NormalizedName,
        *,
        all_yanked: bool,
        merged_specifier: SpecifierSet | None = None,
    ) -> bool:
        """May a yanked version of ``project_name`` be selected?

        :param all_yanked: is every version left in the package's current
            range yanked?
        :param merged_specifier: the requirements merged for this package so
            far. When the engine can supply it this is exactly pip's rule.
            When it cannot, the command line pins are used instead, which
            under-approximates: a pin that arises only from a transitive
            ``==`` requirement is missed and the yanked version is refused
            where pip would take it.
        """
        if not all_yanked:
            return False
        if merged_specifier is not None:
            return is_pinned(merged_specifier)
        return project_name in self._pinned


def solve(
    *,
    inputs: ResolveInputs,
    index: PipHostIndex,
    reporter: NabReporter,
    yank_policy: YankPolicy,
    widening: bool = True,
) -> Solution:
    """Run the engine over ``inputs``, sourcing versions from ``index``.

    :param widening: keep nab's range widening on. It is worth 13 to 15
        percent of resolve time and it can change which of several valid
        solutions is returned, so it is a behaviour switch and not only a
        performance one.
    """
    raise NotImplementedError(ENGINE_MISSING)
