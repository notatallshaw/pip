"""Solver limits and faults stop without another search."""

from typing import Any

import pytest

from pip._vendor.nab_provider import prepared_resolution as session_module
from pip._vendor.nab_provider.preference import IncompletePreferenceError
from pip._vendor.nab_resolver.errors import (
    ResolutionError,
    ResolutionInvariantError,
    ResolutionLimitError,
    ResolutionStalledError,
)
from pip._vendor.nab_resolver.resolver import Resolver as CoreResolver
from pip._vendor.nab_resolver.resolver import ResolverProvider

from pip._internal.exceptions import InstallationError
from pip._internal.index.package_finder import PackageFinder
from pip._internal.operations.prepare import RequirementPreparer
from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.nab.catalogue import PipCatalogue
from pip._internal.resolution.nab.resolver import Resolver


@pytest.fixture
def resolver(preparer: RequirementPreparer, finder: PackageFinder) -> Resolver:
    return Resolver(
        preparer=preparer,
        finder=finder,
        wheel_cache=None,
        make_install_req=install_req_from_line,
        use_user_site=False,
        ignore_dependencies=False,
        only_dependencies=False,
        ignore_installed=False,
        ignore_requires_python=False,
        force_reinstall=False,
        upgrade_strategy="to-satisfy-only",
    )


@pytest.mark.parametrize("requirements", [[], ["probe"]])
def test_real_iteration_limit_is_not_retried(
    resolver: Resolver, monkeypatch: pytest.MonkeyPatch, requirements: list[str]
) -> None:
    solvers = []

    def bounded(
        provider: ResolverProvider[Any, int], **kwargs: Any
    ) -> CoreResolver[Any, int]:
        # Use the real solver with an exhausted allocation.
        kwargs["max_iterations"] = 0
        solver = CoreResolver(provider, **kwargs)
        solvers.append(solver)
        return solver

    monkeypatch.setattr(session_module, "Resolver", bounded)
    collected = resolver.factory.collect_root_requirements(
        [install_req_from_line(requirement) for requirement in requirements]
    )

    with pytest.raises(InstallationError, match="exceeded 0 iterations"):
        resolver._resolve(collected)

    assert len(solvers) == 1


@pytest.mark.parametrize(
    "error_type",
    [
        ResolutionError,
        IncompletePreferenceError,
        ResolutionLimitError,
        ResolutionStalledError,
        ResolutionInvariantError,
    ],
)
def test_terminal_errors_do_not_restart_after_source_discovery(
    resolver: Resolver,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[ResolutionError],
) -> None:
    attempts = 0
    original = error_type("analysis interrupted")

    def stop(catalogue: PipCatalogue, *_args: object, **_kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        catalogue.factory.source_epoch += 1
        raise original

    monkeypatch.setattr(PipCatalogue, "solve", stop)
    collected = resolver.factory.collect_root_requirements([])

    with pytest.raises(InstallationError, match="analysis interrupted") as failure:
        resolver._resolve(collected)

    assert attempts == 1
    assert failure.value.__cause__ is original
