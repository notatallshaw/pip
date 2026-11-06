"""Solver limits and faults stop without another search."""

import logging
from functools import partial
from typing import Any

import pytest

from pip._vendor.nab_provider.preference import (
    IncompletePreferenceError,
    PreferenceScope,
)
from pip._vendor.nab_provider.preference_provider import PreferenceProvider
from pip._vendor.nab_resolver.candidate_provider import CandidateProvider
from pip._vendor.nab_resolver.errors import (
    ResolutionError,
    ResolutionInvariantError,
    ResolutionLimitError,
    ResolutionStalledError,
    ResolutionTerminatedError,
)
from pip._vendor.nab_resolver.resolver import Resolver as CoreResolver
from pip._vendor.nab_resolver.types import Incompatibility, IncompatibilityCause, Term
from pip._vendor.packaging.ranges import VersionRange

from pip._internal.exceptions import InstallationError
from pip._internal.index.package_finder import PackageFinder
from pip._internal.operations.prepare import RequirementPreparer
from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.nab import catalogue as catalogue_module
from pip._internal.resolution.nab import resolver as resolver_module
from pip._internal.resolution.nab.factory import Factory
from pip._internal.resolution.nab.provider import PipProvider
from pip._internal.resolution.nab.ranges import CandidateKey
from pip._internal.resolution.nab.resolver import Resolver
from pip._internal.resolution.nab.yank_candidates import YankCandidates


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
def test_real_iteration_limit_is_neither_certified_nor_retried(
    resolver: Resolver,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    requirements: list[str],
) -> None:
    monkeypatch.setattr(
        catalogue_module, "Resolver", partial(CoreResolver, max_iterations=0)
    )
    collected = resolver.factory.collect_root_requirements(
        [install_req_from_line(requirement) for requirement in requirements]
    )
    with (
        caplog.at_level(logging.INFO),
        pytest.raises(InstallationError, match="exceeded 0 iterations"),
    ):
        resolver._resolve(collected)
    assert "certified failure" not in caplog.text
    assert "catalogue fallback" not in caplog.text
    assert "native retry" not in caplog.text


@pytest.mark.parametrize(
    "error_type", [ResolutionStalledError, ResolutionInvariantError]
)
@pytest.mark.parametrize("requirements", [[], ["probe"]])
def test_diagnostic_graph_does_not_certify_a_solver_fault(
    resolver: Resolver,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_type: type[ResolutionTerminatedError],
    requirements: list[str],
) -> None:
    clause = Incompatibility(
        [Term("probe", VersionRange.full(), positive=True)],
        cause=IncompatibilityCause.NO_VERSIONS,
    )

    def stop(*_args: object, **_kwargs: object) -> None:
        raise error_type("solver state is inconsistent", clause)

    monkeypatch.setattr(CoreResolver, "solve", stop)
    collected = resolver.factory.collect_root_requirements(
        [install_req_from_line(requirement) for requirement in requirements]
    )
    with (
        caplog.at_level(logging.INFO),
        pytest.raises(InstallationError, match="solver state is inconsistent"),
    ):
        resolver._resolve(collected)
    assert "certified failure" not in caplog.text
    assert "catalogue fallback" not in caplog.text


def test_native_limit_does_not_retry_with_a_fresh_budget(
    resolver: Resolver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        resolver_module, "NabResolver", partial(CoreResolver, max_iterations=0)
    )
    collected = resolver.factory.collect_root_requirements([])
    with pytest.raises(InstallationError, match="exceeded 0 iterations"):
        resolver._resolve_native(collected)


@pytest.mark.parametrize("error_type", [ResolutionError, IncompletePreferenceError])
def test_error_without_a_proof_does_not_restart(
    resolver: Resolver,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_type: type[ResolutionError],
) -> None:
    attempts = 0

    def stop(*_args: object, **_kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        raise error_type("analysis interrupted")

    monkeypatch.setattr(CoreResolver, "solve", stop)
    collected = resolver.factory.collect_root_requirements([])
    with caplog.at_level(logging.INFO), pytest.raises(InstallationError):
        resolver._resolve(collected)
    assert attempts == 1
    assert "catalogue fallback" not in caplog.text


def test_native_solver_uses_definitive_candidate_absences(
    resolver: Resolver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    solvers: list[CoreResolver[str, CandidateKey]] = []

    def bounded(
        provider: CandidateProvider[str, CandidateKey], **kwargs: Any
    ) -> CoreResolver[str, CandidateKey]:
        """Create real one-iteration solvers and record every attempt."""
        solver = CoreResolver(provider, max_iterations=1, **kwargs)
        solvers.append(solver)
        return solver

    monkeypatch.setattr(resolver_module, "NabResolver", bounded)
    collected = resolver.factory.collect_root_requirements(
        [install_req_from_line("probe")]
    )
    with pytest.raises(InstallationError, match="exceeded 1 iterations"):
        resolver._resolve_native(collected)
    assert len(solvers) == 1
    assert solvers[0].provisional_absences == 0


def test_proxy_query_limit_keeps_its_terminal_classification(
    factory: Factory,
    provider: PipProvider,
) -> None:
    collected = factory.collect_root_requirements([install_req_from_line("simple")])
    catalogue = catalogue_module.CatalogueProvider(factory, provider, collected)
    candidates = YankCandidates(catalogue)
    proxy = PreferenceProvider.from_catalogue(
        candidates,
        catalogue.roots,
        [],
        catalogue.constraints,
        resolver_factory=candidates.make_resolver,
    )
    proxy.preference.remaining = 1

    with pytest.raises(ResolutionLimitError, match="exceeded 1 iterations"):
        proxy.run_query(PreferenceScope())
