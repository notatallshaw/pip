"""The reporting surface pip expects during a resolve.

pip prints three escalating messages when it keeps rejecting versions of one
package, at the 1st, 8th and 13th rejection, and ``PIP_RESOLVER_DEBUG``
turns every resolver event into a log line. Both come from resolvelib
reporter callbacks that PubGrub has no equivalent for: PubGrub does not
reject candidates one at a time, it derives that a range is impossible.

The counter is therefore driven by version rejections the engine reports:
a package with no versions left in its range, and versions dropped because
their metadata could not be used. The wording and the thresholds are pip's,
unchanged, so the same run produces the same three messages.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pip._vendor.packaging.utils import NormalizedName

    from pip._internal.resolution.resolvelib.base import Constraint

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

    def event(self, name: str, *args: object) -> None:
        """Log a resolver event under ``PIP_RESOLVER_DEBUG``.

        The resolvelib variant has one reporter method per event; nab has one
        observer with a fixed set of callbacks. Rather than pin either shape
        into pip, the engine seam forwards a name and its arguments.
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
