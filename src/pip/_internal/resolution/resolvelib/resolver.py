from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, cast

from pip._vendor.resolvelib import BaseReporter, ResolutionImpossible, ResolutionTooDeep
from pip._vendor.resolvelib import Resolver as RLResolver

from pip._internal.cache import WheelCache
from pip._internal.exceptions import ResolutionTooDeepError
from pip._internal.index.package_finder import PackageFinder
from pip._internal.operations.prepare import RequirementPreparer
from pip._internal.req.req_install import InstallRequirement
from pip._internal.req.req_set import RequirementSet
from pip._internal.resolution._order import (
    _req_set_item_sorter,
    get_topological_weights,
    installation_order,
)
from pip._internal.resolution._reqset import build_requirement_set
from pip._internal.resolution.base import BaseResolver, InstallRequirementProvider
from pip._internal.resolution.resolvelib.provider import PipProvider
from pip._internal.resolution.resolvelib.reporter import (
    PipDebuggingReporter,
    PipReporter,
)

from .base import Candidate, Requirement
from .factory import Factory

__all__ = [
    "Resolver",
    "_req_set_item_sorter",
    "get_topological_weights",
]

if TYPE_CHECKING:
    from pip._vendor.resolvelib.resolvers import Result as RLResult

    Result = RLResult[Requirement, Candidate, str]


logger = logging.getLogger(__name__)


class Resolver(BaseResolver):
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
    ):
        super().__init__()
        assert upgrade_strategy in self._allowed_strategies
        assert not (ignore_dependencies and only_dependencies)

        self.factory = Factory(
            finder=finder,
            preparer=preparer,
            make_install_req=make_install_req,
            wheel_cache=wheel_cache,
            use_user_site=use_user_site,
            force_reinstall=force_reinstall,
            ignore_installed=ignore_installed,
            ignore_requires_python=ignore_requires_python,
            py_version_info=py_version_info,
        )
        self.ignore_dependencies = ignore_dependencies
        self.only_dependencies = only_dependencies
        self.upgrade_strategy = upgrade_strategy
        self._result: Result | None = None

    def resolve(
        self, root_reqs: list[InstallRequirement], check_supported_wheels: bool
    ) -> RequirementSet:
        collected = self.factory.collect_root_requirements(root_reqs)
        provider = PipProvider(
            factory=self.factory,
            constraints=collected.constraints,
            ignore_dependencies=self.ignore_dependencies,
            only_dependencies=self.only_dependencies,
            upgrade_strategy=self.upgrade_strategy,
            user_requested=collected.user_requested,
        )
        if "PIP_RESOLVER_DEBUG" in os.environ:
            reporter: BaseReporter[Requirement, Candidate, str] = PipDebuggingReporter()
        else:
            reporter = PipReporter(constraints=provider.constraints)

        resolver: RLResolver[Requirement, Candidate, str] = RLResolver(
            provider,
            reporter,
        )

        try:
            limit_how_complex_resolution_can_be = 200000
            result = self._result = resolver.resolve(
                collected.requirements, max_rounds=limit_how_complex_resolution_can_be
            )

        except ResolutionImpossible as e:
            error = self.factory.get_installation_error(
                cast("ResolutionImpossible[Requirement, Candidate]", e),
                collected.constraints,
            )
            raise error from e
        except ResolutionTooDeep:
            raise ResolutionTooDeepError from None

        return build_requirement_set(
            result.mapping.values(),
            check_supported_wheels=check_supported_wheels,
            get_dist_to_uninstall=self.factory.get_dist_to_uninstall,
            force_reinstall=self.factory.force_reinstall,
            only_dependencies=self.only_dependencies,
            user_requested=collected.user_requested,
        )

    def get_installation_order(
        self, req_set: RequirementSet
    ) -> list[InstallRequirement]:
        """Get order for installation of requirements in RequirementSet.

        The returned list contains a requirement before another that depends on
        it. This helps ensure that the environment is kept consistent as they
        get installed one-by-one.

        The current implementation creates a topological ordering of the
        dependency graph, giving more weight to packages with less
        or no dependencies, while breaking any cycles in the graph at
        arbitrary points. We make no guarantees about where the cycle
        would be broken, other than it *would* be broken.
        """
        assert self._result is not None, "must call resolve() first"

        return installation_order(req_set, self._result.graph)
