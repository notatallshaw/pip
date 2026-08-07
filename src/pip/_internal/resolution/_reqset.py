"""Turn a resolver's chosen candidates into a :class:`RequirementSet`.

A resolver ends holding a set of chosen candidates, and has to hand pip a
``RequirementSet`` with extras collapsed back onto their base requirement
and ``should_reinstall`` set. That logic lives here rather than inside the
resolver.

The candidate type is a structural protocol rather than
``pip._internal.resolution.model.base.Candidate``, so this module imports
no resolver.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Protocol, TypeVar

from pip._internal.req.constructors import install_req_extend_extras
from pip._internal.req.req_set import RequirementSet
from pip._internal.utils.packaging import get_requirement

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from pip._vendor.packaging.utils import NormalizedName
    from pip._vendor.packaging.version import Version

    from pip._internal.metadata import BaseDistribution
    from pip._internal.models.link import Link
    from pip._internal.req.req_install import InstallRequirement

logger = logging.getLogger(__name__)


class ResolvedCandidate(Protocol):
    """The part of a resolved candidate this module reads.

    ``pip._internal.resolution.model.base.Candidate`` satisfies this
    structurally, and so does the nab adapter's candidate wrapper.
    """

    @property
    def name(self) -> str:
        """Name in the resolver, including any ``[extras]`` part."""

    @property
    def project_name(self) -> NormalizedName:
        """Canonical project name, never carrying an ``[extras]`` part."""

    @property
    def version(self) -> Version: ...

    @property
    def is_editable(self) -> bool: ...

    @property
    def source_link(self) -> Link | None: ...

    def get_install_requirement(self) -> InstallRequirement | None:
        """The ireq to install, or None if nothing is to be installed."""


_CandidateT = TypeVar("_CandidateT", bound=ResolvedCandidate)


def build_requirement_set(
    candidates: Iterable[_CandidateT],
    *,
    check_supported_wheels: bool,
    get_dist_to_uninstall: Callable[[_CandidateT], BaseDistribution | None],
    force_reinstall: bool,
    only_dependencies: bool,
    user_requested: Iterable[str],
) -> RequirementSet:
    """Build the ``RequirementSet`` pip installs from.

    :param candidates: the chosen candidates, in any order.
    :param get_dist_to_uninstall: ``Factory.get_dist_to_uninstall``, which may
        raise ``InstallationError`` for a user-site shadowing conflict.
    :param user_requested: the resolver keys the user asked for on the command
        line, used only by ``--only-deps``.
    """
    req_set = RequirementSet(check_supported_wheels=check_supported_wheels)
    # process candidates with extras last to ensure their base equivalent is
    # already in the req_set if appropriate.
    # Python's sort is stable so using a binary key function keeps relative order
    # within both subsets.
    for candidate in sorted(candidates, key=lambda c: c.name != c.project_name):
        ireq = candidate.get_install_requirement()
        if ireq is None:
            if candidate.name != candidate.project_name:
                # extend existing req's extras
                with contextlib.suppress(KeyError):
                    req = req_set.get_requirement(candidate.project_name)
                    req_set.add_named_requirement(
                        install_req_extend_extras(
                            req, get_requirement(candidate.name).extras
                        )
                    )
            continue

        # Check if there is already an installation under the same name,
        # and set a flag for later stages to uninstall it, if needed.
        installed_dist = get_dist_to_uninstall(candidate)
        if installed_dist is None:
            # There is no existing installation -- nothing to uninstall.
            ireq.should_reinstall = False
        elif force_reinstall:
            # The --force-reinstall flag is set -- reinstall.
            ireq.should_reinstall = True
        elif installed_dist.version != candidate.version:
            # The installation is different in version -- reinstall.
            ireq.should_reinstall = True
        elif candidate.is_editable or installed_dist.editable:
            # The incoming distribution is editable, or different in
            # editable-ness to installation -- reinstall.
            ireq.should_reinstall = True
        elif candidate.source_link and candidate.source_link.is_file:
            # The incoming distribution is under file://
            if candidate.source_link.is_wheel:
                # is a local wheel -- do nothing.
                logger.info(
                    "%s is already installed with the same version as the "
                    "provided wheel. Use --force-reinstall to force an "
                    "installation of the wheel.",
                    ireq.name,
                )
                continue

            # is a local sdist or path -- reinstall
            ireq.should_reinstall = True
        else:
            continue

        link = candidate.source_link
        if link and link.is_yanked:
            # The reason can contain non-ASCII characters, Unicode
            # is required for Python 2.
            msg = (
                "The candidate selected for download or install is a "
                "yanked version: {name!r} candidate (version {version} "
                "at {link})\nReason for being yanked: {reason}"
            ).format(
                name=candidate.name,
                version=candidate.version,
                link=link,
                reason=link.yanked_reason or "<none given>",
            )
            logger.warning(msg)

        req_set.add_named_requirement(ireq)

    if only_dependencies:
        for requested in user_requested:
            project_name = requested.partition("[")[0]
            req_set.requirements.pop(project_name, None)

    return req_set
