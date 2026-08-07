"""The reporting surface pip expects during a resolve.

pip prints three escalating messages when it keeps rejecting versions of one
package, at the 1st, 8th and 13th rejection, and ``PIP_RESOLVER_DEBUG``
turns every resolver event into a log line. Both were written against
per-candidate reporter callbacks that PubGrub has no equivalent for: PubGrub
does not reject candidates one at a time, it derives that a range is
impossible.

The counter is driven by the one PubGrub event that means what pip's does.
pip prints from ``rejecting_candidate``, which fires when a candidate that
was already pinned is discarded because of a conflict; PubGrub's equivalent
is a conflict step whose satisfier is a decision, because that decision is
about to be undone. A version skipped because its metadata could not be read
is not that event, and pip does not count one either. The wording and the
thresholds are pip's, unchanged.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pip._vendor.packaging.utils import NormalizedName

    from pip._internal.resolution.model.base import Constraint

logger = logging.getLogger(__name__)

_MESSAGES_AT_REJECT_COUNT = {
    1: (
        "pip is looking at multiple versions of {package_name} to "
        "determine which version is compatible with other "
        "requirements. This could take a while."
    ),
    8: (
        "pip is still looking at multiple versions of {package_name} to "
        "determine which version is compatible with other "
        "requirements. This could take a while."
    ),
    13: (
        "This is taking longer than usual. You might need to provide "
        "the dependency resolver with stricter constraints to reduce "
        "runtime. See https://pip.pypa.io/warnings/backtracking for "
        "guidance. If you want to abort this run, press Ctrl + C."
    ),
}


class NabReporter:
    """Counts rejections and prints pip's backtracking messages."""

    def __init__(
        self, constraints: Mapping[NormalizedName, Constraint] | None = None
    ) -> None:
        self.reject_count_by_package: defaultdict[str, int] = defaultdict(int)
        self._constraints = constraints or {}
        self._debug = "PIP_RESOLVER_DEBUG" in os.environ

    def rejecting_version(self, package_name: str, detail: str = "") -> None:
        """One version of ``package_name`` was ruled out."""
        self.reject_count_by_package[package_name] += 1
        count = self.reject_count_by_package[package_name]
        message = _MESSAGES_AT_REJECT_COUNT.get(count)
        if message is not None:
            logger.info("INFO: %s", message.format(package_name=package_name))
        if detail:
            logger.debug("Will try a different candidate: %s", detail)

    def no_versions(self, package_name: str, detail: str = "") -> None:
        """No version of ``package_name`` is left in its current range."""
        self.rejecting_version(package_name, detail)

    def event_enabled(self) -> bool:
        """Is ``PIP_RESOLVER_DEBUG`` set?

        Asked once by the engine seam so a run without it never builds the
        argument tuple an event would discard.
        """
        return self._debug

    def event(self, name: str, *args: object) -> None:
        """Log a resolver event under ``PIP_RESOLVER_DEBUG``.

        nab has one observer with a fixed set of callbacks. Rather than pin
        that shape into pip, the engine seam forwards a name and its
        arguments.
        """
        if not self._debug:
            return
        logger.info("Resolver.%s(%s)", name, ", ".join(repr(arg) for arg in args))

    def constraint_text(self, package_name: NormalizedName) -> str:
        """The "user requested (constraint)" line, or an empty string."""
        constraint = self._constraints.get(package_name)
        if constraint is None or not constraint.specifier:
            return ""
        return f"{package_name}{constraint.format_for_error()}"
