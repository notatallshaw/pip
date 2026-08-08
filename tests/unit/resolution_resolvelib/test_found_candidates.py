from __future__ import annotations

import functools
from collections.abc import Callable, Iterator

from pip._vendor.packaging.version import Version

from pip._internal.resolution.resolvelib.base import Candidate
from pip._internal.resolution.resolvelib.found_candidates import (
    FoundCandidates,
    IndexCandidateInfo,
)


class VersionOnlyCandidate(Candidate):
    """A candidate that only knows its version, which is all the ordering uses."""

    def __init__(self, version: str) -> None:
        self._version = Version(version)

    @property
    def version(self) -> Version:
        return self._version


def index_infos(*versions: str) -> Callable[[], Iterator[IndexCandidateInfo]]:
    """Build index candidates for ``versions``, in the order they are given."""

    def get_infos() -> Iterator[IndexCandidateInfo]:
        for version in versions:
            yield Version(version), functools.partial(VersionOnlyCandidate, version)

    return get_infos


def upgrade_order(installed: str, *index_versions: str) -> list[str]:
    candidates = FoundCandidates(
        index_infos(*index_versions),
        VersionOnlyCandidate(installed),
        prefers_installed=False,
        incompatible_ids=set(),
    )
    return [str(candidate.version) for candidate in candidates]


def test_installed_is_inserted_at_its_own_version() -> None:
    assert upgrade_order("2.0", "3.0", "1.5", "1.0") == ["3.0", "2.0", "1.5", "1.0"]


def test_installed_comes_last_when_older_than_every_index_version() -> None:
    assert upgrade_order("1.0", "3.0", "2.0") == ["3.0", "2.0", "1.0"]


def test_installed_comes_first_when_newer_than_every_index_version() -> None:
    assert upgrade_order("4.0", "3.0", "2.0") == ["4.0", "3.0", "2.0"]


def test_installed_is_inserted_once_when_index_is_not_version_ordered() -> None:
    assert upgrade_order("2.0", "1.5", "1.0", "3.0") == ["2.0", "1.5", "1.0", "3.0"]
