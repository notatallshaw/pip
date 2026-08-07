"""Why a package had no usable version, in a form a host can re-word.

The provider records one :class:`NoVersionsReason` per package it could
not place.  ``str()`` on it is nab's own sentence; an embedder reads
:attr:`NoVersionsReason.kind` and the counts behind it and writes its own,
which is what lets a host keep wording its own tests assert on.

The types here hold values only.  They import nothing from the provider,
so the module sits at the bottom of the engine's import graph and a host
can read a reason without importing the resolve path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pip._vendor.packaging.version import Version
    from pip._vendor.nab_resolver.types import RangeProtocol


class NoVersionsKind(Enum):
    """What stopped the search from picking a version.

    The first three are answers about the index itself, the next three
    are about nab's listing filter, and the last two are about the
    search: ``ALL_REJECTED`` means candidates matched the requirement
    and look-ahead refused every one, ``NO_MATCH`` means none matched.
    """

    NOT_FOUND = "not-found"
    OFFLINE_SKIPPED = "offline-skipped"
    UNREADABLE_FORMATS = "unreadable-formats"
    WHEEL_TAGS = "wheel-tags"
    FILTERED = "filtered"
    FILTERED_IN_RANGE = "filtered-in-range"
    ALL_REJECTED = "all-rejected"
    NO_MATCH = "no-match"


class BlockerKind(Enum):
    """Why look-ahead refused a candidate.

    ``DECIDED`` is a package the solution has already pinned elsewhere,
    ``SOLUTION_RANGE`` and ``ROOT_RANGE`` are range disagreements with
    the partial solution and with a root requirement, and ``METADATA``
    is a candidate whose metadata could not be read at all.
    """

    DECIDED = "decided"
    SOLUTION_RANGE = "solution-range"
    ROOT_RANGE = "root-range"
    METADATA = "metadata"


@dataclass(slots=True)
class ListingDrops:
    """What the listing filter removed for one package, by cause.

    Counts files, except ``no_sdist_under_sdist_install``, which counts
    releases: the drop is decided per release once its artifacts are
    known.  Every field is package-wide and target-wide, so a reason
    that scopes itself to a version range must say so rather than quote
    these as the range's own numbers.

    ``no_upload_time`` and ``unparseable_upload_time`` are split out of
    ``uploaded_after_cutoff`` because they are not the same event: the
    cutoff rejected nothing, the index published nothing to compare it
    against.  PEP 700 makes the field optional, so nab excludes rather
    than raises, and this is the only place that says so.
    """

    invalid_version: int = 0
    dist_policy: int = 0
    requires_python: int = 0
    uploaded_after_cutoff: int = 0
    no_upload_time: int = 0
    unparseable_upload_time: int = 0
    no_sdist_under_sdist_install: int = 0
    wheel_tags: int = 0

    def clauses(self, *, include_wheel_tags: bool = True) -> tuple[str, ...]:
        """Render one phrase per cause that fired, in a fixed order.

        Empty when nothing was counted, which is what a caller checks
        before quoting them.
        """
        out = []
        for name, noun, phrase, _ in _DROP_PHRASES:
            if name == "wheel_tags" and not include_wheel_tags:
                continue
            count = getattr(self, name)
            if count:
                out.append(f"{_counted(count, noun)} {phrase}")
        return tuple(out)

    def sole_cause(self) -> str | None:
        """Name the one cause that fired, or ``None`` when it was not one.

        A caller scoped to part of the package (one version range, say)
        cannot quote these counts, because they are package-wide.  It can
        quote the cause when exactly one fired, because then every file
        this package lost, it lost to that one.
        """
        causes = [cause for name, _, _, cause in _DROP_PHRASES if getattr(self, name)]
        if len(causes) != 1:
            return None
        return causes[0]


# field, counted noun, counted phrase, bare cause name.
_DROP_PHRASES = (
    ("requires_python", "file", "rejected by requires-python", "requires-python"),
    ("wheel_tags", "wheel", "rejected by the target's tags", "the target's wheel tags"),
    ("dist_policy", "file", "rejected by dist-policy", "dist-policy"),
    (
        "uploaded_after_cutoff",
        "file",
        "uploaded after the cutoff",
        "the upload-time cutoff",
    ),
    (
        "no_upload_time",
        "file",
        "with no upload time on the index",
        "having no upload time on the index",
    ),
    (
        "unparseable_upload_time",
        "file",
        "with an unparseable upload time",
        "an unparseable upload time",
    ),
    (
        "invalid_version",
        "file",
        "with an unparseable version",
        "an unparseable version",
    ),
    (
        "no_sdist_under_sdist_install",
        "release",
        "with no sdist under dist-policy sdist-install",
        "dist-policy sdist-install, which needs an sdist",
    ),
)


def _counted(count: int, noun: str) -> str:
    """Return ``count`` with ``noun`` pluralised."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


@dataclass(frozen=True, slots=True)
class Blocker:
    """One reason look-ahead refused the candidates of a package.

    ``package`` is the package the blocker is about: the already-decided
    or disagreeing dependency for the first three kinds, and the
    candidate's own name for ``METADATA``.  ``count`` and ``message``
    are only set for ``METADATA``, which collapses one entry per failing
    version into a single blocker carrying how many failed and what the
    first one said.
    """

    kind: BlockerKind
    package: str
    version: Version | None = None
    wanted: RangeProtocol[Version] | None = None
    held: RangeProtocol[Version] | None = None
    count: int = 1
    message: str | None = None

    def __str__(self) -> str:
        """Render nab's own phrase for this blocker."""
        if self.kind is BlockerKind.DECIDED:
            return f"requires {self.package} != {self.version}"
        if self.kind is BlockerKind.SOLUTION_RANGE:
            return (
                f"requires {self.package} in {self.wanted}"
                f" but solution has it in {self.held}"
            )
        if self.kind is BlockerKind.ROOT_RANGE:
            return (
                f"requires {self.package} in {self.wanted}"
                f" but root has it in {self.held}"
            )
        if self.count == 1:
            return f"{self.message}"
        return (
            f"{self.count} versions failed metadata extraction (first: {self.message})"
        )


@dataclass(frozen=True, slots=True)
class NoVersionsReason:
    """Why one package contributed a NO_VERSIONS clause.

    ``drops`` is filled for the listing-filter kinds and is otherwise all
    zeros; a host that owns its own index layer (so nab's listing filter
    never runs) sees ``NOT_FOUND``, ``NO_MATCH`` and ``ALL_REJECTED``
    only.  ``sdist_filtered`` qualifies ``WHEEL_TAGS``: an sdist was on
    the index and the pre-tag filter took it, so the user is not being
    told to bring a source that is already there.
    """

    kind: NoVersionsKind
    drops: ListingDrops = field(default_factory=ListingDrops)
    blockers: tuple[Blocker, ...] = ()
    sdist_filtered: bool = False

    def __str__(self) -> str:
        """Render nab's own sentence for this reason."""
        fixed = _FIXED_SENTENCES.get(self.kind)
        if fixed is not None:
            return fixed
        if self.kind is NoVersionsKind.WHEEL_TAGS:
            return self._wheel_tags_sentence()
        if self.kind is NoVersionsKind.FILTERED:
            return (
                f"found on index but no distribution is compatible ({self._causes()})"
            )
        if self.kind is NoVersionsKind.FILTERED_IN_RANGE:
            return (
                "found on index but every version matching the requirement"
                f" was filtered ({self._range_cause()})"
            )
        joined = "; ".join(str(blocker) for blocker in self.blockers)
        return f"every version in range was rejected: {joined}"

    def _range_cause(self) -> str:
        """Name the filter, when the package-wide tally can only mean one.

        The counts are package-wide and this sentence is about one version
        range, so the numbers cannot be quoted.  The cause can be, and only
        when exactly one fired.
        """
        cause = self.drops.sole_cause()
        if cause is None:
            return "by requires-python, wheel tags, dist-policy, or upload-time"
        return f"by {cause}"

    def _wheel_tags_sentence(self) -> str:
        head = (
            "found on index but none of the wheel's tags are compatible"
            " with the resolve target"
            f" ({_counted(self.drops.wheel_tags, 'wheel')} rejected)"
        )
        if not self.sdist_filtered:
            return f"{head}, and no sdist is available to build from"
        base = self.drops.clauses(include_wheel_tags=False)
        if not base:
            return (
                f"{head}, and the sdist was filtered by requires-python,"
                f" dist-policy, or upload-time"
            )
        return f"{head}, and the sdist was filtered ({', '.join(base)})"

    def _causes(self) -> str:
        clauses = self.drops.clauses()
        if not clauses:
            # Nothing was tallied, so name the filters that could have run
            # rather than assert a cause the counts do not support.
            return "all filtered by requires-python, dist-policy, or upload-time"
        return ", ".join(clauses)


# The kinds whose sentence is the same every time.  The other three read
# the counts or the blockers, so they are built in ``__str__``.
_FIXED_SENTENCES = {
    NoVersionsKind.NOT_FOUND: "package not found on any configured index",
    NoVersionsKind.OFFLINE_SKIPPED: (
        "offline mode skipped an index with no cached listing"
    ),
    NoVersionsKind.UNREADABLE_FORMATS: (
        "found on index but no file is a wheel or a .tar.gz sdist"
        " (the formats nab reads)"
    ),
    NoVersionsKind.NO_MATCH: "no version matches the requirement",
}
