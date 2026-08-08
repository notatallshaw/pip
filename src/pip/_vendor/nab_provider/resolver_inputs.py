"""Turn a project's PEP 508 requirements into the resolver's input shape.

One step, drawn as its own module because it is the only part of a
resolve that is pure: requirements plus a marker environment in, a
``{key: VersionRange}`` dict out, with no index, no coordinator and no
provider involved.  The engine calls it once per target for the
requirements and once for the constraints.

It takes a :class:`~nab_provider.vcs_admission.VcsConfig` rather than the whole
project config, because deciding whether a direct-URL requirement is admitted
at all is the entirety of what it asks the config.

Evaluating a root requirement's marker is the caller's, not this module's:
:data:`MarkerHolds` arrives as an argument.  The predicate needs a
set-valued ``extra`` and so needs :mod:`packaging.markersets`, which is the
one part of packaging a host embedding the engine would otherwise have to
carry.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from pip._vendor.packaging.ranges import VersionRange
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.nab_resolver.errors import ResolutionError

from .conflict_kind import membership_set_in_marker
from .errors import ConfigError
from .extra_keys import join_extra, split_extra
from .vcs_admission import admit_vcs_url

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from pip._vendor.packaging.markers import Marker
    from pip._vendor.packaging.requirements import Requirement

    from .vcs_admission import VcsConfig

    # Whether a dependency marker holds for one environment.
    # :func:`nab_provider.marker_holds.dependency_marker_holds` is nab's own;
    # a host embedding the engine supplies its own instead.
    MarkerHolds = Callable[[Marker, Mapping[str, str]], bool]


_logger = logging.getLogger(__name__)


def raise_for_unsatisfiable(
    ranges: Mapping[str, VersionRange],
    sources: Mapping[str, Sequence[str]],
    *,
    kind: str,
) -> None:
    """Raise :class:`ResolutionError` if any folded range is empty.

    ``ranges`` holds one intersected range per package and ``sources``
    the requirement strings folded into each.  An empty range means
    those requirements share no version; the error lists them.

    ``kind`` ("requirement" or "constraint") only shapes the wording.
    """
    unsatisfiable = [name for name, range_ in ranges.items() if range_.is_empty]
    if not unsatisfiable:
        return
    detail = "\n".join(
        f"  {name}: {', '.join(sources[name])}" for name in unsatisfiable
    )
    msg = f"conflicting {kind}s leave no satisfiable version:\n{detail}"
    raise ResolutionError(msg)


def _warn_dropped_root_marker(req: Requirement, warned: set[str]) -> None:
    """Warn when a dropped root requirement tests an extra/group membership.

    A root marker testing ``extra``, ``extras``, or ``dependency_groups``
    evaluates False at resolve time (root activates no extra or group), so the
    dep would otherwise be dropped silently.  ``warned`` carries the
    requirements already reported in this run, so one mistaken requirement is
    reported once rather than once per target per fork.
    """
    marker_text = str(req.marker)
    if "extra ==" not in marker_text and not membership_set_in_marker(marker_text):
        return
    text = str(req)
    if text in warned:
        return
    warned.add(text)
    _logger.warning(
        "Root requirement %r tests an extra or dependency-group membership "
        "marker; the dep is dropped because root activates no extra or group "
        "at resolve time. For an extra, use pkg[extra] (extras-of-package).",
        text,
    )


def build_resolver_inputs(
    requirements: Sequence[Requirement],
    vcs: VcsConfig,
    *,
    environment: Mapping[str, str],
    marker_holds: MarkerHolds,
    kind: str = "requirement",
    warned: set[str] | None = None,
) -> tuple[dict[str, VersionRange], set[tuple[str, str]]]:
    """Convert PEP 508 requirements to the resolver's input shape.

    Requirements whose PEP 508 marker ``marker_holds`` rejects under
    ``environment`` are skipped, matching pip/uv's root-requirement
    handling.  Repeated package names are intersected into one range;
    an empty intersection raises :class:`ResolutionError`.  A direct-URL
    or VCS requirement is refused by :func:`admit_vcs_url` under ``vcs``;
    resolving one is not implemented.

    ``kind`` is ``"requirement"`` or ``"constraint"``.  A constraint may
    not carry extras, and shapes the error wording; the returned extras
    set is empty for one.

    ``warned`` is the run's set of already-reported extra/group root
    markers (see :func:`_warn_dropped_root_marker`); a caller that does
    not share one gets a fresh set, so it warns per call.
    """
    resolver_requirements: dict[str, VersionRange] = {}
    sources: defaultdict[str, list[str]] = defaultdict(list)
    root_extras: set[tuple[str, str]] = set()
    already_warned = set() if warned is None else warned
    for req in requirements:
        if kind == "constraint" and req.extras:
            msg = f"Constraints cannot have extras: {req}"
            raise ConfigError(msg)
        if req.marker is not None and not marker_holds(req.marker, environment):
            _warn_dropped_root_marker(req, already_warned)
            continue
        if req.url is not None:
            admit_vcs_url(req.url, vcs)
            msg = (
                f"VCS {kind} admitted by policy but resolver path is not"
                f" implemented: {req.name} @ {req.url}"
            )
            raise NotImplementedError(msg)
        name = str(canonicalize_name(req.name))
        previous = resolver_requirements.get(name, VersionRange.full())
        term = (
            req.specifier.to_range()
            if req.specifier
            else VersionRange.full(admit_arbitrary=False)
        )
        resolver_requirements[name] = previous & term
        sources[name].append(str(req))
        for extra in sorted(req.extras):
            extra_key = join_extra(name, extra)
            resolver_requirements[extra_key] = VersionRange.full(admit_arbitrary=False)
            _, normalized_extra = split_extra(extra_key)
            assert normalized_extra is not None  # join_extra always sets one
            root_extras.add((name, normalized_extra))
    raise_for_unsatisfiable(resolver_requirements, sources, kind=kind)
    return resolver_requirements, root_extras


def extend_constraints_to_proxies(
    constraints: dict[str, VersionRange],
    root_extras: set[tuple[str, str]],
) -> None:
    """Copy each base package's constraint onto its extras proxies.

    The resolver keys constraints by the package it is deciding, and an
    extras proxy decides under its own ``name[extra]`` key, so the base's
    constraint does not otherwise reach it.  Sharing the key also keeps
    the proxy on the constraint-attribution path, so a constraint that
    leaves it nothing is named in the failure.
    """
    for name, extra in root_extras:
        constraint = constraints.get(name)
        if constraint is not None:
            constraints[join_extra(name, extra)] = constraint
