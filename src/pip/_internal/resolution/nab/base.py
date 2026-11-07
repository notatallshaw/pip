from __future__ import annotations

import typing
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pip._vendor.nab_resolver.values import SlottedValue
from pip._vendor.packaging.specifiers import SpecifierSet
from pip._vendor.packaging.utils import NormalizedName, canonicalize_name
from pip._vendor.packaging.version import Version

from pip._internal.models.link import Link, LinkHash, links_equivalent
from pip._internal.req.req_install import InstallRequirement
from pip._internal.utils.hashes import FAVORITE_HASH, Hashes

if TYPE_CHECKING:
    from typing_extensions import override
else:
    override = getattr(typing, "override", lambda method: method)


def format_name(project: NormalizedName, extras: frozenset[NormalizedName]) -> str:
    if not extras:
        return project
    extras_expr = ",".join(sorted(extras))
    return f"{project}[{extras_expr}]"


def intersect_hash_options(
    left: dict[str, list[str]], right: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Intersect declarations while preserving an empty set of permitted hashes."""
    left = {
        algorithm: list(dict.fromkeys(value.lower() for value in values))
        for algorithm, values in left.items()
    }
    right = {
        algorithm: list(dict.fromkeys(value.lower() for value in values))
        for algorithm, values in right.items()
    }
    if not left:
        return {algorithm: list(values) for algorithm, values in right.items()}
    if not right:
        return {algorithm: list(values) for algorithm, values in left.items()}
    shared = {
        algorithm: [value for value in values if value in right[algorithm]]
        for algorithm, values in left.items()
        if algorithm in right
    }
    return shared or {FAVORITE_HASH: []}


def input_hash_options(ireq: InstallRequirement) -> dict[str, list[str]]:
    """Collect checksums declared in input flags and the original URL."""
    options: dict[str, list[str]] = {
        algorithm: list(values) for algorithm, values in ireq.hash_options.items()
    }
    link = ireq.original_link
    if isinstance(link, Link) and link.hash is not None:
        assert link.hash_name is not None
        options.setdefault(link.hash_name, []).append(link.hash)
    return options


@cache
def source_link_without_hashes(link: Link) -> Link:
    """Return a source URL without fragment checksum hints."""
    parsed = urlsplit(link.url)
    fragment = []
    for name, value in parse_qsl(parsed.fragment, keep_blank_values=True):
        checksum = LinkHash.find_hash_url_fragment(f"#{name}=")
        if checksum is None or checksum.name != name:
            fragment.append((name, value))
    return Link(urlunsplit(parsed._replace(fragment=urlencode(fragment))))


def source_links_equivalent(left: Link, right: Link) -> bool:
    """Compare archive locations separately from checksum requirements."""
    return links_equivalent(
        source_link_without_hashes(left), source_link_without_hashes(right)
    )


@dataclass(frozen=True)
class Constraint:
    specifier: SpecifierSet
    hashes: Hashes
    hash_options: dict[str, list[str]]
    links: frozenset[Link]

    @classmethod
    def empty(cls) -> Constraint:
        return Constraint(SpecifierSet(), Hashes(), {}, frozenset())

    @classmethod
    def from_ireq(cls, ireq: InstallRequirement) -> Constraint:
        links = frozenset([ireq.link]) if ireq.link else frozenset()
        hash_options = input_hash_options(ireq)
        return Constraint(
            ireq.specifier,
            Hashes(hash_options),
            hash_options,
            links,
        )

    def __bool__(self) -> bool:
        return bool(self.specifier) or bool(self.hashes) or bool(self.links)

    def __and__(self, other: InstallRequirement) -> Constraint:
        if not isinstance(other, InstallRequirement):
            return NotImplemented
        specifier = self.specifier & other.specifier
        other_hash_options = input_hash_options(other)
        other_hashes = Hashes(other_hash_options)
        hashes = self.hashes & other_hashes
        if self.hashes and other_hashes and not hashes:
            hashes = Hashes({FAVORITE_HASH: []})
        hash_options = intersect_hash_options(self.hash_options, other_hash_options)
        links = self.links
        if other.link:
            links = links.union([other.link])
        return Constraint(specifier, hashes, hash_options, links)

    def is_satisfied_by(self, candidate: Candidate) -> bool:
        # Reject if there are any mismatched URL constraints on this package.
        if self.links and not all(_match_link(link, candidate) for link in self.links):
            return False
        # We can safely always allow prereleases here since PackageFinder
        # already implements the prerelease logic, and would have filtered out
        # prerelease candidates if the user does not expect them.
        return self.specifier.contains(candidate.version, prereleases=True)

    def format_for_error(self) -> str:
        s = str(self.specifier)
        if self.links:
            s += f" (from {', '.join(str(link) for link in self.links)})"
        return s


class Requirement(SlottedValue):
    """An original declaration, an exact candidate, or an interpreter bound."""

    __match_args__ = ("name", "candidate", "specifier")
    __slots__ = (
        "name",
        "project_name",
        "candidate",
        "ireq",
        "specifier",
        "is_python",
        "_display",
        "_identity",
    )

    def __init__(
        self,
        name: str,
        project_name: NormalizedName,
        *,
        candidate: Candidate | None = None,
        ireq: InstallRequirement | None = None,
        specifier: SpecifierSet | None = None,
        is_python: bool = False,
        display: str,
        identity: tuple[object, ...],
    ) -> None:
        self.name = name
        self.project_name = project_name
        self.candidate = candidate
        self.ireq = ireq
        self.specifier = specifier
        self.is_python = is_python
        self._display = display
        self._identity = identity

    @override
    def identity(self) -> tuple[object, ...]:
        return self._identity

    @override
    def __str__(self) -> str:
        return self._display

    @classmethod
    def from_install_req(cls, ireq: InstallRequirement) -> Requirement:
        assert ireq.req is not None and ireq.link is None
        project = canonicalize_name(ireq.req.name)
        name = format_name(
            project, frozenset(canonicalize_name(extra) for extra in ireq.extras)
        )
        return cls(
            name,
            project,
            ireq=ireq,
            specifier=ireq.specifier,
            display=str(ireq.req),
            identity=("specifier", name, str(ireq)),
        )

    @classmethod
    def from_candidate(cls, candidate: Candidate) -> Requirement:
        return cls(
            candidate.name,
            candidate.project_name,
            candidate=candidate,
            display=candidate.format_for_error(),
            identity=("candidate", candidate),
        )

    @classmethod
    def for_python(cls, specifier: SpecifierSet, candidate: Candidate) -> Requirement:
        return cls(
            candidate.name,
            candidate.project_name,
            candidate=candidate,
            specifier=specifier,
            is_python=True,
            display=f"Python {specifier}",
            identity=("python", str(specifier), candidate),
        )

    @classmethod
    def unavailable(
        cls,
        name: NormalizedName,
        *,
        source_requirement: InstallRequirement | None = None,
    ) -> Requirement:
        display = (
            str(source_requirement.req)
            if source_requirement is not None
            else f"{name} (unavailable)"
        )
        return cls(name, name, display=display, identity=("unavailable", name, display))

    def get_candidate_lookup(
        self,
    ) -> tuple[Candidate | None, InstallRequirement | None]:
        if self.candidate is not None:
            if self.specifier is not None and not self.specifier.contains(
                self.candidate.version, prereleases=True
            ):
                return None, None
            return self.candidate, None
        return None, self.ireq

    def is_satisfied_by(self, candidate: Candidate) -> bool:
        if candidate.name != self.name:
            return False
        if self.specifier is not None:
            return self.specifier.contains(candidate.version, prereleases=True)
        return self.candidate is not None and candidate == self.candidate

    def format_for_error(self) -> str:
        if self.ireq is None:
            return self._display
        parts = self._display.split(",")
        return (
            ", ".join(parts[:-1]) + " and " + parts[-1]
            if len(parts) > 1
            else self._display
        )


def _match_link(link: Link, candidate: Candidate) -> bool:
    if candidate.source_link:
        return source_links_equivalent(link, candidate.source_link)
    return False


class Candidate:
    @property
    def project_name(self) -> NormalizedName:
        """The "project name" of the candidate.

        This is different from ``name`` if this candidate contains extras,
        in which case ``name`` would contain the ``[...]`` part, while this
        refers to the name of the project.
        """
        raise NotImplementedError("Override in subclass")

    @property
    def name(self) -> str:
        """The name identifying this candidate in the resolver.

        This is different from ``project_name`` if this candidate contains
        extras, where ``project_name`` would not contain the ``[...]`` part.
        """
        raise NotImplementedError("Override in subclass")

    @property
    def version(self) -> Version:
        raise NotImplementedError("Override in subclass")

    @property
    def is_installed(self) -> bool:
        raise NotImplementedError("Override in subclass")

    @property
    def is_editable(self) -> bool:
        raise NotImplementedError("Override in subclass")

    @property
    def source_link(self) -> Link | None:
        raise NotImplementedError("Override in subclass")

    def iter_dependencies(self, with_requires: bool) -> Iterable[Requirement | None]:
        raise NotImplementedError("Override in subclass")

    def get_install_requirement(self) -> InstallRequirement | None:
        raise NotImplementedError("Override in subclass")

    def format_for_error(self) -> str:
        raise NotImplementedError("Subclass should override")
