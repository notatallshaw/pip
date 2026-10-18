"""Read project-local development sources without changing package metadata."""

from __future__ import annotations

import copy
import hashlib
import logging
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from pip._vendor.packaging.dependency_sources import (
    DependencySource,
    InvalidDependencySource,
    parse_dependency_sources,
    select_dependency_source,
)
from pip._vendor.packaging.markers import Marker
from pip._vendor.packaging.specifiers import SpecifierSet
from pip._vendor.packaging.utils import canonicalize_name

from pip._internal.exceptions import InstallationError
from pip._internal.models.link import Link
from pip._internal.models.target_python import TargetPython
from pip._internal.req.req_install import InstallRequirement
from pip._internal.utils.compat import tomllib
from pip._internal.utils.misc import redact_auth_from_url

logger = logging.getLogger(__name__)
_CREDENTIAL = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_IMPLEMENTATIONS: dict[str, dict[str, str]] = {
    "cp": {
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
    },
    "pp": {"implementation_name": "pypy", "platform_python_implementation": "PyPy"},
}


@dataclass(frozen=True)
class Source(DependencySource):
    """An installer source, resolved against its declaring file."""

    marker: Marker | None = field(default=None, compare=False, hash=False)
    extra: str = field(default="", compare=False)
    group: str = field(default="", compare=False)
    declared_in: Path = field(default=Path(), compare=False)

    def __str__(self) -> str:
        return f"{self.kind} {redact_auth_from_url(self.location)}"

    def as_link(self) -> Link:
        location = self.location
        if self.kind != "path":
            location = _expand_credentials(location, str(self.declared_in))
        if self.kind == "path":
            url = Path(location).as_uri()
        elif self.kind in {"url", "index"}:
            url = location
        else:
            url = f"{self.kind}+{location}"
            if self.revision:
                url += "@" + self.revision
        if self.subdirectory:
            separator = "&" if "#" in url else "#"
            url += separator + "subdirectory=" + quote(self.subdirectory, safe="/")
        return Link(url, comes_from=str(self.declared_in))


def _expand_credentials(url: str, where: str) -> str:
    parts = urlsplit(url)
    credentials, sep, host = parts.netloc.rpartition("@")
    if not sep:
        return url

    def expand(match: re.Match[str]) -> str:
        name = match[1]
        if name not in os.environ:
            raise InstallationError(f"{where}: environment variable {name} is not set")
        return quote(os.environ[name], safe="")

    credentials = _CREDENTIAL.sub(expand, credentials)
    return urlunsplit(parts._replace(netloc=f"{credentials}@{host}"))


def _prepare_source(entry: DependencySource, path: Path, name: str) -> Source:
    """Check filesystem and hash support before adapting a parsed declaration."""
    location = entry.location
    subdirectory = entry.subdirectory
    where = f"{path}: develop.sources.{name}"
    if entry.kind == "path":
        local = Path(location)
        if local.is_absolute():
            logger.warning("%s uses an absolute path: %s", where, location)
        local = (path.parent / local).resolve()
        if local.is_dir() and subdirectory:
            local = (local / subdirectory).resolve()
            subdirectory = ""
        if entry.editable and not local.is_dir():
            raise InstallationError(f"{where}: editable sources must be directories")
        if entry.hashes and local.is_dir():
            raise InstallationError(f"{where}: directory sources cannot have hashes")
        location = str(local)

    hashes: list[tuple[str, str]] = []
    for algorithm, digest in entry.hashes:
        try:
            hasher = hashlib.new(algorithm)
        except ValueError:
            continue
        if not hasher.digest_size or len(digest) != hasher.digest_size * 2:
            raise InstallationError(
                f"{where}: {algorithm} has an invalid digest length"
            )
        hashes.append((algorithm, digest))
    if entry.hashes and not hashes:
        raise InstallationError(f"{where}: no supported hash algorithm")
    return Source(
        kind=entry.kind,
        location=location,
        editable=entry.editable,
        subdirectory=subdirectory,
        revision=entry.revision,
        hashes=tuple(hashes),
        marker=entry.marker,
        extra=entry.extra,
        group=entry.group,
        declared_in=path,
    )


def read_sources(path: Path) -> dict[str, tuple[Source, ...]]:
    """Read one explicit source table and resolve paths against its own file."""
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, ValueError) as exc:
        raise InstallationError(
            f"Cannot read dependency sources from {path}: {exc}"
        ) from exc
    develop = data.get("develop", {})
    if not isinstance(develop, dict) or develop.keys() - {"sources"}:
        raise InstallationError(f"{path}: develop must contain only the sources table")
    try:
        sources = parse_dependency_sources(develop.get("sources", {}))
    except InvalidDependencySource as exc:
        raise InstallationError(f"{path}: {exc}") from exc
    return {
        name: tuple(_prepare_source(entry, path, name) for entry in entries)
        for name, entries in sources.items()
    }


def project_file_path(path: str | Path) -> Path:
    """Resolve the containing directory without following a manifest symlink."""
    path = Path(path)
    return path.parent.resolve() / path.name


class DependencySources:
    """One file's development sources and selection context for a resolution."""

    def __init__(self, target: Path) -> None:
        self.target = project_file_path(target)
        self.entries = read_sources(self.target)
        self.root_groups: dict[InstallRequirement, tuple[Path, str]] = {}
        self.environment: dict[str, str] = {}

    def set_target_environment(self, target: TargetPython) -> None:
        """Use explicit target information for source selectors, refusing ambiguity."""
        versions = target.get_marker_version_overrides()
        self.environment.update(versions)
        implementation = sys.implementation.name
        if target.implementation is not None:
            declared = _IMPLEMENTATIONS.get(target.implementation)
            if declared is not None:
                self.environment.update(declared)
                implementation = declared["implementation_name"]
            elif any(
                self._uses_marker_variable(name)
                for name in (
                    "implementation_name",
                    "platform_python_implementation",
                    "implementation_version",
                )
            ):
                raise InstallationError(
                    "Source markers need a known target implementation"
                )
        if versions and implementation == "cpython":
            self.environment["implementation_version"] = versions["python_full_version"]
        elif (
            versions or implementation != sys.implementation.name
        ) and self._uses_marker_variable("implementation_version"):
            raise InstallationError(
                "Cannot infer source marker implementation_version for this target"
            )
        platforms = target.platforms
        if platforms is None or not platforms:
            return
        environments = [_platform_environment(platform) for platform in platforms]
        for variable in (
            "sys_platform",
            "os_name",
            "platform_system",
            "platform_machine",
        ):
            values = {environment.get(variable) for environment in environments}
            if len(values) == 1 and None not in values:
                self.environment[variable] = next(
                    value for value in values if value is not None
                )
            elif self._uses_marker_variable(variable):
                raise InstallationError(
                    f"Source marker {variable} needs a single known target platform"
                )
        for variable in ("platform_release", "platform_version"):
            if self._uses_marker_variable(variable):
                raise InstallationError(
                    f"Source marker {variable} cannot be inferred from --platform"
                )

    def _uses_marker_variable(self, variable: str) -> bool:
        return any(
            entry.marker is not None
            and re.search(rf"\b{variable}\b", str(entry.marker)) is not None
            for entries in self.entries.values()
            for entry in entries
        )

    def is_target(self, requirement: InstallRequirement | None) -> bool:
        if (
            requirement is None
            or requirement.link is None
            or not requirement.link.is_file
        ):
            return False
        return (
            Path(requirement.link.file_path).resolve() == self.target.parent.resolve()
        )

    def select(self, name: str, *, extra: str = "", group: str = "") -> Source | None:
        entry = select_dependency_source(
            self.entries.get(canonicalize_name(name), ()),
            extra=extra,
            group=group,
            environment=self.environment,
        )
        if entry is None or entry.kind == "path":
            return entry
        location = _expand_credentials(entry.location, str(entry.declared_in))
        return (
            entry if location == entry.location else replace(entry, location=location)
        )

    def for_requirement(
        self, ireq: InstallRequirement, requested_extras: Iterable[str]
    ) -> tuple[Source | None, ...]:
        """Select sources in the target's extra/group context or the default context."""
        assert ireq.req is not None
        if ireq.link is not None and (
            ireq.user_supplied
            or self.is_target(
                ireq.comes_from
                if isinstance(ireq.comes_from, InstallRequirement)
                else None
            )
        ):
            if canonicalize_name(ireq.req.name) in self.entries:
                logger.warning(
                    "Ignoring source for %s: the target's direct reference wins",
                    ireq.req.name,
                )
            return (None,)
        contexts = [("", "")]
        if ireq in self.root_groups:
            path, group = self.root_groups[ireq]
            if path == self.target:
                contexts = [("", canonicalize_name(group))]
        elif isinstance(ireq.comes_from, InstallRequirement) and self.is_target(
            ireq.comes_from
        ):
            contexts = (
                [("", "")]
                if ireq.markers is None or ireq.markers.evaluate({"extra": ""})
                else []
            )
            if ireq.markers is not None and "extra" in str(ireq.markers):
                contexts.extend(
                    (canonicalize_name(extra), "")
                    for extra in requested_extras
                    if ireq.markers.evaluate({"extra": extra})
                )
        selections = dict.fromkeys(
            self.select(ireq.req.name, extra=extra, group=group)
            for extra, group in contexts
        )
        return tuple(selections)

    def apply(
        self, requirement: InstallRequirement, source: Source
    ) -> InstallRequirement:
        """Redirect a copied input without changing the original requirement."""
        assert requirement.req is not None
        result = copy.copy(requirement)
        result.req = copy.copy(requirement.req)
        if source.kind == "index":
            result.req.url = None
            result.link = result.original_link = None
        else:
            result.link = result.original_link = source.as_link()
            result.req.url = result.link.url
            result.req.specifier = SpecifierSet()
            result.editable = source.editable
            result.source_dir = source.location if source.editable else None
            result.local_file_path = (
                result.link.file_path if result.link.is_file else None
            )
            result.source_hashes = dict(source.hashes)
        logger.info(
            "Using source for %s: %s (from %s)",
            requirement.req.name,
            source,
            source.declared_in,
        )
        return result

    def warn_unused(self, applied: set[str]) -> None:
        """Report declarations absent from the successful resolution's sources."""
        for name in sorted(self.entries.keys() - applied):
            logger.warning(
                "No development source was applied for %s (from %s)",
                name,
                self.entries[name][0].declared_in,
            )


def choose_sources(
    requirements: Iterable[InstallRequirement],
    groups: Iterable[tuple[str, str]],
    explicit: str | None,
) -> DependencySources | None:
    """Read one explicit file or an unambiguous local project/group input."""
    if explicit is not None:
        path = Path(explicit)
        if path.is_dir():
            path /= "pyproject.toml"
        return DependencySources(path)
    targets = {project_file_path(path) for path, _ in groups}
    for requirement in requirements:
        link = requirement.link
        if (
            not requirement.constraint
            and link is not None
            and link.is_file
            and link.is_existing_dir()
        ):
            path = Path(link.file_path) / "pyproject.toml"
            if path.is_file():
                targets.add(project_file_path(path))
    sources = [DependencySources(path) for path in sorted(targets)]
    if not any(source.entries for source in sources):
        return None
    if len(sources) > 1:
        paths = ", ".join(str(source.target) for source in sources)
        raise InstallationError(
            f"Multiple source files were selected: {paths}. "
            "Use --sources PATH to choose one, or --no-sources to ignore them."
        )
    return sources[0]


def _platform_environment(platform: str) -> dict[str, str]:
    """Translate common wheel platforms into the marker values they identify."""
    if platform == "win32" or platform.startswith("win_"):
        machine = {"win32": "x86", "win_amd64": "AMD64", "win_arm64": "ARM64"}.get(
            platform
        )
        result = {
            "sys_platform": "win32",
            "os_name": "nt",
            "platform_system": "Windows",
        }
    elif platform.startswith(("linux_", "manylinux", "musllinux")):
        machine = next(
            (
                name
                for name in (
                    "x86_64",
                    "aarch64",
                    "armv7l",
                    "i686",
                    "ppc64le",
                    "s390x",
                    "riscv64",
                )
                if platform.endswith("_" + name)
            ),
            None,
        )
        result = {
            "sys_platform": "linux",
            "os_name": "posix",
            "platform_system": "Linux",
        }
    elif platform.startswith("macosx_"):
        machine = next(
            (name for name in ("x86_64", "arm64") if platform.endswith("_" + name)),
            None,
        )
        result = {
            "sys_platform": "darwin",
            "os_name": "posix",
            "platform_system": "Darwin",
        }
    else:
        return {}
    if machine is not None:
        result["platform_machine"] = machine
    return result
