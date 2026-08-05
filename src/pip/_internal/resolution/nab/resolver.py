"""Resolution backed by nab, pip's third resolver variant.

Selected with ``--use-feature=nab-resolver``. This module is the plumbing
only: constructing the resolver succeeds so that the wiring can be tested,
and every method that would need the nab engine raises
:class:`NotImplementedError` naming what is still missing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pip._internal.resolution.base import BaseResolver

if TYPE_CHECKING:
    from pip._internal.cache import WheelCache
    from pip._internal.index.package_finder import PackageFinder
    from pip._internal.operations.prepare import RequirementPreparer
    from pip._internal.req.req_install import InstallRequirement
    from pip._internal.req.req_set import RequirementSet
    from pip._internal.resolution.base import InstallRequirementProvider


NOT_IMPLEMENTED_MESSAGE = (
    "The nab resolver is not implemented yet. Selecting it with "
    "--use-feature=nab-resolver reaches this point and stops. Still missing: "
    "the vendored nab engine, the pip provider that feeds it candidates and "
    "dependencies, and the translation of its answer back into pip's "
    "RequirementSet and installation order."
)


class Resolver(BaseResolver):
    """Placeholder for the nab-backed resolver.

    The constructor accepts and records exactly what
    ``pip._internal.resolution.resolvelib.resolver.Resolver`` accepts, so the
    selection path can be exercised end to end before an engine exists.
    """

    _allowed_strategies = {"eager", "only-if-needed", "to-satisfy-only"}

    def __init__(
        self,
        preparer: RequirementPreparer,
        finder: PackageFinder,
        wheel_cache: WheelCache | None,
        make_install_req: InstallRequirementProvider,
        use_user_site: bool,
        ignore_dependencies: bool,
        only_dependencies: bool,
        ignore_installed: bool,
        ignore_requires_python: bool,
        force_reinstall: bool,
        upgrade_strategy: str,
        py_version_info: tuple[int, ...] | None = None,
    ) -> None:
        super().__init__()
        assert upgrade_strategy in self._allowed_strategies

        self.preparer = preparer
        self.finder = finder
        self.wheel_cache = wheel_cache
        self.make_install_req = make_install_req
        self.use_user_site = use_user_site
        self.ignore_dependencies = ignore_dependencies
        self.only_dependencies = only_dependencies
        self.ignore_installed = ignore_installed
        self.ignore_requires_python = ignore_requires_python
        self.force_reinstall = force_reinstall
        self.upgrade_strategy = upgrade_strategy
        self.py_version_info = py_version_info

    def resolve(
        self, root_reqs: list[InstallRequirement], check_supported_wheels: bool
    ) -> RequirementSet:
        raise NotImplementedError(NOT_IMPLEMENTED_MESSAGE)

    def get_installation_order(
        self, req_set: RequirementSet
    ) -> list[InstallRequirement]:
        raise NotImplementedError(NOT_IMPLEMENTED_MESSAGE)
