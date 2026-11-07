from __future__ import annotations

import typing
from logging import getLogger
from typing import TYPE_CHECKING

from pip._vendor.nab_resolver.resolver import ResolverObserver

from .base import Candidate

if TYPE_CHECKING:
    from pip._vendor.packaging.version import Version

if TYPE_CHECKING:
    from typing_extensions import override
else:
    override = getattr(typing, "override", lambda method: method)

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

    def candidate_considered(self, candidate: Candidate) -> None:
        versions = self._versions.setdefault(candidate.project_name, set())
        if candidate.version in versions:
            return

        # Repeated queries and extra views do not add alternative versions.
        versions.add(candidate.version)
        alternatives = len(versions) - 1
        message = _MESSAGES_AT_ALTERNATIVE_COUNT.get(alternatives)
        if message is not None:
            logger.info("INFO: %s", message.format(package_name=candidate.project_name))


class PipDebuggingReporter(ResolverObserver[str, Candidate]):
    """Log the start and selected candidates of a resolution attempt."""

    def starting(self) -> None:
        logger.info("Reporter.starting()")

    @override
    def on_decision(self, package: str, version: Candidate, level: int) -> None:
        logger.info("Reporter.pinning(%r)", version)
