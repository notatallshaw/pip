"""Resolution backed by nab, pip's third resolver variant.

Selected with ``--use-feature=nab-resolver``. pip owns the index layer, the
installed environment and every install decision; nab owns the search. The
division is deliberate: because every candidate probe costs exactly what a
probe costs pip today, a benchmark against the resolvelib variant measures
search quality and nothing else.

What lives where:

- :mod:`.inputs` splits pip's root ireqs into requirements, constraints and
  explicit link requirements.
- :mod:`.candidates` supplies the candidate universe and the metadata,
  entirely out of pip's finder, factory and preparer.
- :mod:`.observer` keeps pip's backtracking messages.
- :mod:`.errors` rebuilds pip's error sentences from the engine's causes.
- :mod:`.engine` is the only module that will import nab, and it is the only
  one that still raises ``NotImplementedError``.
- ``resolution._reqset`` and ``resolution._order`` are shared with the
  resolvelib variant, so reinstall decisions and installation order cannot
  drift between the two.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pip._internal.resolution._order import MutableGraph, installation_order
from pip._internal.resolution._reqset import build_requirement_set
from pip._internal.resolution.base import BaseResolver
from pip._internal.resolution.nab.candidates import PipHostIndex
from pip._internal.resolution.nab.engine import EngineFailure, YankPolicy, solve
from pip._internal.resolution.nab.errors import to_installation_error
from pip._internal.resolution.nab.inputs import collect_inputs
from pip._internal.resolution.nab.observer import NabReporter
from pip._internal.resolution.resolvelib.factory import Factory

if TYPE_CHECKING:
    from pip._vendor.packaging.utils import NormalizedName
    from pip._vendor.packaging.version import Version

    from pip._internal.cache import WheelCache
    from pip._internal.index.package_finder import PackageFinder
    from pip._internal.operations.prepare import RequirementPreparer
    from pip._internal.req.req_install import InstallRequirement
    from pip._internal.req.req_set import RequirementSet
    from pip._internal.resolution.base import InstallRequirementProvider
    from pip._internal.resolution.nab.candidates import HostCandidate
    from pip._internal.resolution.nab.engine import Solution

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
    ) -> None:
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
        self._finder = finder
        self._make_install_req = make_install_req
        self.ignore_dependencies = ignore_dependencies
        self.only_dependencies = only_dependencies
        self.upgrade_strategy = upgrade_strategy
        self._solution: Solution | None = None
        self._index: PipHostIndex | None = None

    def resolve(
        self, root_reqs: list[InstallRequirement], check_supported_wheels: bool
    ) -> RequirementSet:
        inputs = collect_inputs(
            root_reqs,
            ignore_dependencies=self.ignore_dependencies,
            name_link=self._name_link_requirement,
        )
        index = self._index = PipHostIndex(
            factory=self.factory,
            finder=self._finder,
            inputs=inputs,
            upgrade_strategy=self.upgrade_strategy,
            make_install_req=self._make_install_req,
        )
        reporter = NabReporter(constraints=inputs.constraints)

        try:
            solution = self._solution = solve(
                inputs=inputs,
                index=index,
                reporter=reporter,
                yank_policy=YankPolicy(inputs.pinned_packages()),
                python_version=self.factory._python_candidate.version,
                ignore_requires_python=self.factory._ignore_requires_python,
            )
        except EngineFailure as exc:
            logger.debug("nab could not resolve:\n%s", exc)
            raise to_installation_error(
                exc.causes, factory=self.factory, index=index, inputs=inputs
            ) from exc

        return build_requirement_set(
            [
                index.pip_candidate(
                    self._host_candidate(index, pin.project_name, pin.version),
                    pin.extras,
                )
                for pin in solution.pins
            ],
            check_supported_wheels=check_supported_wheels,
            get_dist_to_uninstall=self.factory.get_dist_to_uninstall,
            force_reinstall=self.factory.force_reinstall,
            only_dependencies=self.only_dependencies,
            user_requested=inputs.user_requested,
        )

    def get_installation_order(
        self, req_set: RequirementSet
    ) -> list[InstallRequirement]:
        """Get order for installation of requirements in RequirementSet.

        The returned list contains a requirement before another that depends
        on it. The engine records the edges it derived, and they are walked
        by the same weighting the resolvelib variant uses.
        """
        assert self._solution is not None, "must call resolve() first"

        graph = MutableGraph.from_edges(self._solution.edges)
        return installation_order(req_set, graph)

    def _name_link_requirement(self, ireq: InstallRequirement) -> NormalizedName:
        """Name a URL, path or VCS requirement by preparing it.

        ``pip install ./some/path`` has no name until the distribution has
        been built. pip resolves that by building the candidate and reading
        the name off it, and an unnamed URL that fails to build is a hard
        error rather than something the search can back out of, because the
        user typed it.
        """
        assert ireq.link is not None
        candidate = self.factory._make_base_candidate_from_link(
            ireq.link,
            template=ireq,
            name=None,
            version=None,
        )
        if candidate is None:
            raise self.factory._build_failures[ireq.link]
        return candidate.project_name

    @staticmethod
    def _host_candidate(
        index: PipHostIndex, project_name: NormalizedName, version: Version
    ) -> HostCandidate:
        for host_candidate in index.candidates(project_name):
            if host_candidate.version == version:
                return host_candidate
        raise AssertionError(
            f"the engine pinned {project_name} {version}, which is not in the "
            "candidate universe pip supplied"
        )
