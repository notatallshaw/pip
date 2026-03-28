"""Version interval arithmetic for specifier sets.

This module provides the interval infrastructure used by
``SpecifierSet.is_unsatisfiable()`` and ``SpecifierSet.version_intervals``.
It is an internal module; the public API is on :class:`~.specifiers.SpecifierSet`.
"""

from __future__ import annotations

import enum
import functools
from typing import TYPE_CHECKING, Union

from .version import Version

if TYPE_CHECKING:
    from typing import Any, Iterable


# The smallest possible PEP 440 version.
_MIN_VERSION: Version = Version("0.dev0")


def _trim_release(release: tuple[int, ...]) -> tuple[int, ...]:
    end = len(release)
    while end > 1 and release[end - 1] == 0:
        end -= 1
    return release if end == len(release) else release[:end]


class _BoundaryKind(enum.Enum):
    AFTER_LOCALS = enum.auto()
    AFTER_POSTS = enum.auto()


@functools.total_ordering
class _BoundaryVersion:
    """Synthetic version marking a boundary between version families.

    ``AFTER_LOCALS``: sorts after V and all V+local, before V.post0.
    ``AFTER_POSTS``: sorts after all V.postN, before the next release segment.
    """

    __slots__ = ("_kind", "_trimmed_release", "version")

    def __init__(self, version: Version, kind: _BoundaryKind) -> None:
        self.version = version
        self._kind = kind
        self._trimmed_release = _trim_release(version.release)

    def _is_family(self, other: Version) -> bool:
        v = self.version
        if not (
            other.epoch == v.epoch
            and _trim_release(other.release) == self._trimmed_release
            and other.pre == v.pre
        ):
            return False
        if self._kind == _BoundaryKind.AFTER_LOCALS:
            return other.post == v.post and other.dev == v.dev
        return other.dev == v.dev or other.post is not None

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _BoundaryVersion):
            return self.version == other.version and self._kind == other._kind
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, _BoundaryVersion):
            if self.version != other.version:
                return self.version < other.version
            return self._kind.value < other._kind.value
        return not self._is_family(other) and self.version < other  # type: ignore[operator]

    def __hash__(self) -> int:
        return hash((self.version, self._kind))

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.version!r}, {self._kind.name})"


_VersionOrBoundary = Union[Version, _BoundaryVersion, None]


@functools.total_ordering
class _LowerBound:
    __slots__ = ("inclusive", "version")

    def __init__(self, version: _VersionOrBoundary, inclusive: bool) -> None:
        self.version = version
        self.inclusive = inclusive

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _LowerBound):
            return NotImplemented
        return self.version == other.version and self.inclusive == other.inclusive

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, _LowerBound):
            return NotImplemented
        if self.version is None:
            return other.version is not None
        if other.version is None:
            return False
        if self.version != other.version:
            return self.version < other.version
        return self.inclusive and not other.inclusive

    def __hash__(self) -> int:
        return hash((self.version, self.inclusive))

    def __repr__(self) -> str:
        bracket = "[" if self.inclusive else "("
        return f"<{self.__class__.__name__} {bracket}{self.version!r}>"


@functools.total_ordering
class _UpperBound:
    __slots__ = ("inclusive", "version")

    def __init__(self, version: _VersionOrBoundary, inclusive: bool) -> None:
        self.version = version
        self.inclusive = inclusive

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _UpperBound):
            return NotImplemented
        return self.version == other.version and self.inclusive == other.inclusive

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, _UpperBound):
            return NotImplemented
        if self.version is None:
            return False
        if other.version is None:
            return True
        if self.version != other.version:
            return self.version < other.version
        return not self.inclusive and other.inclusive

    def __hash__(self) -> int:
        return hash((self.version, self.inclusive))

    def __repr__(self) -> str:
        bracket = "]" if self.inclusive else ")"
        return f"<{self.__class__.__name__} {self.version!r}{bracket}>"


SpecifierInterval = tuple[_LowerBound, _UpperBound]

_NEG_INF = _LowerBound(None, False)
_POS_INF = _UpperBound(None, False)
_FULL_RANGE: list[SpecifierInterval] = [(_NEG_INF, _POS_INF)]


def _interval_is_empty(lower: _LowerBound, upper: _UpperBound) -> bool:
    if lower.version is None or upper.version is None:
        return False
    if lower.version == upper.version:
        return not (lower.inclusive and upper.inclusive)
    return lower.version > upper.version


def _intersect_intervals(
    left: list[SpecifierInterval],
    right: list[SpecifierInterval],
) -> list[SpecifierInterval]:
    result: list[SpecifierInterval] = []
    li = ri = 0
    while li < len(left) and ri < len(right):
        ll, lu = left[li]
        rl, ru = right[ri]
        lower = max(ll, rl)
        upper = min(lu, ru)
        if not _interval_is_empty(lower, upper):
            result.append((lower, upper))
        if lu < ru:
            li += 1
        else:
            ri += 1
    return result


def _next_prefix_dev0(version: Version) -> Version:
    release = (*version.release[:-1], version.release[-1] + 1)
    return version.__replace__(
        release=release, pre=None, post=None, dev=0, local=None
    )


def _base_dev0(version: Version) -> Version:
    return version.__replace__(pre=None, post=None, dev=0, local=None)


def specifier_to_intervals(operator: str, version_str: str) -> list[SpecifierInterval]:
    """Convert a single specifier (operator + version string) to intervals."""
    if operator == "===":
        return list(_FULL_RANGE)

    if version_str.endswith(".*"):
        return _wildcard_intervals(operator, version_str)
    return _standard_intervals(operator, version_str)


def _wildcard_intervals(op: str, ver_str: str) -> list[SpecifierInterval]:
    base = Version(ver_str[:-2])
    lower = _base_dev0(base)
    upper = _next_prefix_dev0(base)
    if op == "==":
        return [(_LowerBound(lower, True), _UpperBound(upper, False))]
    # !=
    return [
        (_NEG_INF, _UpperBound(lower, False)),
        (_LowerBound(upper, True), _POS_INF),
    ]


def _standard_intervals(op: str, ver_str: str) -> list[SpecifierInterval]:
    v = Version(ver_str)
    has_local = "+" in ver_str
    after_locals = _BoundaryVersion(v, _BoundaryKind.AFTER_LOCALS)

    if op == ">=":
        return [(_LowerBound(v, True), _POS_INF)]

    if op == "<=":
        return [(_NEG_INF, _UpperBound(after_locals, True))]

    if op == ">":
        if v.dev is not None:
            lower_ver = v.__replace__(dev=v.dev + 1, local=None)
            return [(_LowerBound(lower_ver, True), _POS_INF)]
        if v.is_postrelease:
            assert v.post is not None
            lower_ver = v.__replace__(post=v.post + 1, dev=0, local=None)
            return [(_LowerBound(lower_ver, True), _POS_INF)]
        return [
            (
                _LowerBound(_BoundaryVersion(v, _BoundaryKind.AFTER_POSTS), False),
                _POS_INF,
            )
        ]

    if op == "<":
        bound = v if v.is_prerelease else v.__replace__(dev=0, local=None)
        if bound <= _MIN_VERSION:
            return []
        return [(_NEG_INF, _UpperBound(bound, False))]

    if op == "==":
        eq_upper = v if has_local else after_locals
        return [(_LowerBound(v, True), _UpperBound(eq_upper, True))]

    if op == "!=":
        ne_upper = v if has_local else after_locals
        return [
            (_NEG_INF, _UpperBound(v, False)),
            (_LowerBound(ne_upper, False), _POS_INF),
        ]

    if op == "~=":
        prefix = v.__replace__(release=v.release[:-1])
        return [
            (_LowerBound(v, True), _UpperBound(_next_prefix_dev0(prefix), False))
        ]

    raise ValueError(f"Unknown operator: {op!r}")


class VersionIntervals:
    """Immutable, hashable representation of version ranges satisfying a specifier.

    Two ``VersionIntervals`` objects are equal when they describe the same set of
    versions, regardless of the specifier strings that produced them.
    """

    __slots__ = ("_intervals", "_hash")

    def __init__(self, intervals: tuple[SpecifierInterval, ...]) -> None:
        self._intervals = intervals
        self._hash = hash(intervals)

    @property
    def is_empty(self) -> bool:
        return not self._intervals

    def __bool__(self) -> bool:
        return bool(self._intervals)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VersionIntervals):
            return NotImplemented
        return self._intervals == other._intervals

    def __hash__(self) -> int:
        return self._hash

    def __and__(self, other: VersionIntervals) -> VersionIntervals:
        if not isinstance(other, VersionIntervals):
            return NotImplemented  # type: ignore[return-value]
        result = _intersect_intervals(list(self._intervals), list(other._intervals))
        return VersionIntervals(tuple(result))

    def __contains__(self, item: object) -> bool:
        if not isinstance(item, Version):
            return NotImplemented  # type: ignore[return-value]
        for lower, upper in self._intervals:
            if lower.version is not None and (
                item < lower.version
                or (item == lower.version and not lower.inclusive)
            ):
                break
            if (
                upper.version is None
                or item < upper.version
                or (item == upper.version and upper.inclusive)
            ):
                return True
        return False

    def __repr__(self) -> str:
        parts = []
        for lower, upper in self._intervals:
            lb = "[" if lower.inclusive else "("
            ub = "]" if upper.inclusive else ")"
            lv = str(lower.version) if lower.version is not None else "-inf"
            uv = str(upper.version) if upper.version is not None else "+inf"
            parts.append(f"{lb}{lv}, {uv}{ub}")
        return f"VersionIntervals({', '.join(parts)})"


def compute_intervals(
    specs: Iterable[tuple[str, str]],
) -> list[SpecifierInterval] | None:
    """Compute intersected intervals from an iterable of (operator, version) pairs.

    Returns None if any spec uses === (can't model arbitrary string matching).
    Returns empty list if unsatisfiable.
    """
    result: list[SpecifierInterval] | None = None
    for op, ver in specs:
        if op == "===":
            return None
        intervals = specifier_to_intervals(op, ver)
        if result is None:
            result = intervals
        else:
            result = _intersect_intervals(result, intervals)
            if not result:
                break
    return result if result is not None else list(_FULL_RANGE)
