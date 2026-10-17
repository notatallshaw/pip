# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.

"""Validate development source tables and select entries for an environment."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TypeVar
from urllib.parse import urlsplit

from .markers import InvalidMarker, Marker
from .utils import InvalidName, canonicalize_name

__all__ = [
    "DependencySource",
    "InvalidDependencySource",
    "parse_dependency_sources",
    "select_dependency_source",
]


def __dir__() -> list[str]:
    return __all__


logger = logging.getLogger(__name__)
_KINDS = frozenset({"path", "git", "hg", "bzr", "svn", "url", "index"})
_SELECTORS = frozenset({"marker", "extra", "group"})


class InvalidDependencySource(ValueError):
    """A source table does not follow the development source format."""


@dataclass(frozen=True, kw_only=True, slots=True)
class DependencySource:
    """A source declaration, with paths and credentials left uninterpreted."""

    kind: str
    location: str
    editable: bool = False
    subdirectory: str = ""
    revision: str = ""
    hashes: tuple[tuple[str, str], ...] = ()
    marker: Marker | None = None
    extra: str = ""
    group: str = ""

    def matches(
        self,
        *,
        extra: str = "",
        group: str = "",
        environment: Mapping[str, str] | None = None,
    ) -> bool:
        """Evaluate the selectors in an explicit dependency occurrence's context."""
        extra = canonicalize_name(extra)
        group = canonicalize_name(group)
        return (
            (not self.extra or self.extra == extra)
            and (not self.group or self.group == group)
            and (
                self.marker is None
                or self.marker.evaluate({**(environment or {}), "extra": extra})
            )
        )


def _string(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidDependencySource(f"{where} must be a non-empty string")
    return value


def _hashes(value: object, where: str) -> tuple[tuple[str, str], ...]:
    """Validate digest syntax without choosing the installer's supported algorithms."""
    if not isinstance(value, Mapping) or not value:
        raise InvalidDependencySource(
            f"{where} must be a non-empty table of hash algorithms and digests"
        )
    hashes: list[tuple[str, str]] = []
    for algorithm, digest in value.items():
        if not isinstance(algorithm, str) or not re.fullmatch(r"[a-z0-9_]+", algorithm):
            raise InvalidDependencySource(
                f"{where}: invalid hash algorithm {algorithm!r}"
            )
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]+", digest):
            raise InvalidDependencySource(
                f"{where}.{algorithm} must be a hexadecimal digest"
            )
        hashes.append((algorithm, digest.lower()))
    return tuple(sorted(hashes))


def _parse_source(value: object, name: str) -> DependencySource:
    where = f"develop.sources.{name}"
    if isinstance(value, str):
        value = {"path": value}
    if not isinstance(value, Mapping):
        raise InvalidDependencySource(f"{where} must be a source table or path string")
    kinds = _KINDS.intersection(value)
    if len(kinds) != 1:
        raise InvalidDependencySource(f"{where} must specify exactly one source kind")
    kind = next(iter(kinds))
    allowed = {kind, *_SELECTORS}
    if kind != "index":
        allowed.add("subdirectory")
    if kind == "path":
        allowed.update({"editable", "hashes"})
    elif kind == "url":
        allowed.add("hashes")
    elif kind in {"git", "hg", "bzr", "svn"}:
        allowed.add("rev")
        if kind == "git":
            allowed.update({"tag", "branch"})
    if not all(isinstance(key, str) for key in value):
        raise InvalidDependencySource(f"{where} keys must be strings")
    if unknown := value.keys() - allowed:
        raise InvalidDependencySource(f"{where} has unknown keys: {sorted(unknown)}")
    location = _string(value[kind], f"{where}.{kind}")
    strings = {
        key: _string(value[key], f"{where}.{key}")
        for key in {"subdirectory", "rev", "tag", "branch", *_SELECTORS} & value.keys()
    }
    if "extra" in value and "group" in value:
        raise InvalidDependencySource(
            f"{where} cannot combine extra and group selectors"
        )
    revisions = {"rev", "tag", "branch"} & value.keys()
    if len(revisions) > 1:
        raise InvalidDependencySource(f"{where} cannot combine rev, tag and branch")
    revision = strings.get("rev", "")
    if "tag" in value:
        revision = "refs/tags/" + strings["tag"]
    elif "branch" in value:
        revision = "refs/heads/" + strings["branch"]
    subdirectory = strings.get("subdirectory", "")
    if subdirectory and (
        subdirectory.startswith("/")
        or "\\" in subdirectory
        or ".." in subdirectory.split("/")
    ):
        raise InvalidDependencySource(
            f"{where}.subdirectory must stay within the source"
        )
    editable = value.get("editable", False)
    if not isinstance(editable, bool):
        raise InvalidDependencySource(f"{where}.editable must be a boolean")
    if kind != "path":
        try:
            if kind in {"git", "hg", "bzr", "svn"} and location.startswith(kind + "+"):
                raise InvalidDependencySource(
                    f"{where}.{kind} must omit the {kind}+ prefix"
                )
            if not urlsplit(location).scheme:
                raise InvalidDependencySource(f"{where}.{kind} must be an absolute URL")
        except ValueError as exc:
            raise InvalidDependencySource(f"{where}.{kind} is not a valid URL") from exc
    source_path = location if kind == "path" else urlsplit(location).path
    if subdirectory and source_path.endswith(".whl"):
        raise InvalidDependencySource(f"{where}: wheels cannot have a subdirectory")
    try:
        marker = Marker(strings["marker"]) if "marker" in value else None
    except InvalidMarker as exc:
        raise InvalidDependencySource(f"{where}.marker is invalid: {exc}") from exc
    return DependencySource(
        kind=kind,
        location=location,
        editable=editable,
        subdirectory=subdirectory,
        revision=revision,
        hashes=_hashes(value["hashes"], where + ".hashes") if "hashes" in value else (),
        marker=marker,
        extra=canonicalize_name(strings["extra"]) if "extra" in value else "",
        group=canonicalize_name(strings["group"]) if "group" in value else "",
    )


def parse_dependency_sources(value: object) -> dict[str, tuple[DependencySource, ...]]:
    """Parse the contents of a ``[develop.sources]`` table, without reading files."""
    if not isinstance(value, Mapping):
        raise InvalidDependencySource("develop.sources must be a table")
    sources: dict[str, tuple[DependencySource, ...]] = {}
    for name, raw in value.items():
        if not isinstance(name, str):
            raise InvalidDependencySource("Source package names must be strings")
        try:
            normalized = canonicalize_name(name, validate=True)
        except InvalidName as exc:
            raise InvalidDependencySource(
                f"Invalid source package name {name!r}"
            ) from exc
        if normalized in sources:
            raise InvalidDependencySource(f"Duplicate normalized source name {name!r}")
        entries = raw if isinstance(raw, list) else [raw]
        if not entries:
            raise InvalidDependencySource(f"Source {name!r} cannot be an empty array")
        sources[normalized] = tuple(_parse_source(entry, name) for entry in entries)
        for entry in sources[normalized][:-1]:
            if entry.marker is None and not entry.extra and not entry.group:
                logger.warning(
                    "Entries after the unconditional %s source are unreachable", name
                )
                break
    return sources


_SourceT = TypeVar("_SourceT", bound=DependencySource)


def select_dependency_source(
    sources: Iterable[_SourceT],
    *,
    environment: Mapping[str, str] | None = None,
    extra: str = "",
    group: str = "",
) -> _SourceT | None:
    """Return the first matching entry, or None when no source applies.

    :param sources: Source declarations, in selection order.
    :type sources: ~collections.abc.Iterable[DependencySource]
    :returns: An entry from ``sources``, retaining its concrete type.
    :rtype: DependencySource | None
    """
    return next(
        (
            source
            for source in sources
            if source.matches(extra=extra, group=group, environment=environment)
        ),
        None,
    )
