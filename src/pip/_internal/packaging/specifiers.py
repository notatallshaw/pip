from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pip._vendor.packaging.specifiers import SpecifierSet

    from pip._internal.resolution.resolvelib.base import CandidateVersion


@lru_cache(maxsize=None)
def specifier_contains(
    specifier: "SpecifierSet",
    candidate_version: "CandidateVersion",
    prereleases: bool,
):
    if specifier.contains(candidate_version, prereleases=prereleases):
        return True
    return False
