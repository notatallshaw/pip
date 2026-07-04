from __future__ import annotations

import functools
import logging
from collections.abc import Set as AbstractSet

from pip._vendor.packaging import specifiers, version
from pip._vendor.packaging._parser import MarkerList, Variable
from pip._vendor.packaging.markers import Marker
from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.utils import canonicalize_name

logger = logging.getLogger(__name__)


def _marker_leaf_extra(node: object) -> str | None:
    """If a marker AST leaf compares the ``extra`` variable, return the
    canonicalized name it is compared against, otherwise ``None``."""
    if not isinstance(node, tuple):
        return None
    lhs, _op, rhs = node
    if isinstance(lhs, Variable) and lhs.value == "extra":
        return canonicalize_name(rhs.value)
    if isinstance(rhs, Variable) and rhs.value == "extra":
        return canonicalize_name(lhs.value)
    return None


def _evaluate_marker_against_extras(
    markers: MarkerList, extras: AbstractSet[str]
) -> bool:
    """Evaluate a parsed marker tree, resolving every ``extra`` comparison
    against the *whole* set of requested ``extras`` rather than a single value.

    ``extra == "x"`` is treated as ``"x" in extras`` and ``extra != "x"`` as
    ``"x" not in extras``; any other operator on ``extra`` evaluates to ``False``
    per the dependency-specifiers specification. Comparisons that do not involve
    ``extra`` are delegated to packaging's normal evaluator against the current
    environment.
    """
    groups: list[list[bool]] = [[]]
    for marker in markers:
        if isinstance(marker, list):
            groups[-1].append(_evaluate_marker_against_extras(marker, extras))
        elif isinstance(marker, tuple):
            name = _marker_leaf_extra(marker)
            if name is not None:
                op = marker[1].value
                if op == "==":
                    groups[-1].append(name in extras)
                elif op == "!=":
                    groups[-1].append(name not in extras)
                else:
                    # Other operators on ``extra`` are undefined; per the
                    # dependency-specifiers spec, tools should treat them as
                    # False.
                    groups[-1].append(False)
            else:
                groups[-1].append(Marker._from_markers([marker]).evaluate())
        elif marker == "or":
            groups.append([])
        # "and" joins within the current group and needs no handling.
    return any(all(group) for group in groups)


def _markers_reference_extra(markers: MarkerList) -> bool:
    for marker in markers:
        if isinstance(marker, list):
            if _markers_reference_extra(marker):
                return True
        elif isinstance(marker, tuple) and _marker_leaf_extra(marker) is not None:
            return True
    return False


def marker_references_extra(marker: Marker) -> bool:
    """Return whether ``marker`` compares against the ``extra`` variable."""
    return _markers_reference_extra(marker._markers)


def evaluate_marker_with_extras(marker: Marker, extras: AbstractSet[str]) -> bool:
    """Evaluate ``marker`` treating ``extra`` comparisons as set membership
    against ``extras`` (the full set of extras requested for the package).

    An empty ``extras`` set corresponds to a package requested without any
    extras; ``extra != "x"`` then evaluates ``True`` and ``extra == "x"``
    evaluates ``False``, matching the base (no-extra) install.
    """
    normalized = frozenset(canonicalize_name(e) for e in extras if e)
    return _evaluate_marker_against_extras(marker._markers, normalized)


@functools.lru_cache(maxsize=32)
def check_requires_python(
    requires_python: str | None, version_info: tuple[int, ...]
) -> bool:
    """
    Check if the given Python version matches a "Requires-Python" specifier.

    :param version_info: A 3-tuple of ints representing a Python
        major-minor-micro version to check (e.g. `sys.version_info[:3]`).

    :return: `True` if the given Python version satisfies the requirement.
        Otherwise, return `False`.

    :raises InvalidSpecifier: If `requires_python` has an invalid format.
    """
    if requires_python is None:
        # The package provides no information
        return True
    requires_python_specifier = specifiers.SpecifierSet(requires_python)

    python_version = version.parse(".".join(map(str, version_info)))
    return python_version in requires_python_specifier


@functools.lru_cache(maxsize=10000)
def get_requirement(req_string: str) -> Requirement:
    """Construct a packaging.Requirement object with caching"""
    # Parsing requirement strings is expensive, and is also expected to happen
    # with a low diversity of different arguments (at least relative the number
    # constructed). This method adds a cache to requirement object creation to
    # minimize repeated parsing of the same string to construct equivalent
    # Requirement objects.
    return Requirement(req_string)
