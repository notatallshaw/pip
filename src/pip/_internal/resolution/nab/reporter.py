from __future__ import annotations

from logging import getLogger
from typing import TYPE_CHECKING

from .base import Candidate

if TYPE_CHECKING:
    from pip._vendor.nab_provider.version_candidates import VersionCandidate
    from pip._vendor.packaging.version import Version

logger = getLogger(__name__)

_MESSAGES_AT_ALTERNATIVE_COUNT = {
    2: (
        "pip is looking at multiple versions of {package_name} to "
        "determine which version is compatible with other requirements. "
        "This could take a while."
    ),
    8: (
        "pip is still looking at multiple versions of {package_name} to "
        "determine which version is compatible with other requirements. "
        "This could take a while."
    ),
    13: (
        "This is taking longer than usual. You might need to provide "
        "the dependency resolver with stricter constraints to reduce "
        "runtime. See https://pip.pypa.io/warnings/backtracking for "
        "guidance. If you want to abort this run, press Ctrl + C."
    ),
}


class PipProgressReporter:
    """Report distinct version choices made during dependency search."""

    def __init__(self) -> None:
        self._versions: dict[str, set[Version]] = {}

    def candidate_considered(self, candidate: VersionCandidate) -> None:
        versions = self._versions.setdefault(candidate.base, set())
        if candidate.version in versions:
            return

        # Repeated queries and extra views do not add alternative versions.
        versions.add(candidate.version)
        alternatives = len(versions) - 1
        message = _MESSAGES_AT_ALTERNATIVE_COUNT.get(alternatives)
        if message is not None:
            logger.info("INFO: %s", message.format(package_name=candidate.base))


class PipDebuggingReporter:
    """Log the start and selected candidates of a resolution attempt."""

    def starting(self) -> None:
        logger.info("Reporter.starting()")

    def pinning(self, candidate: Candidate) -> None:
        logger.info("Reporter.pinning(%r)", candidate)
