"""Solver limits and proofless faults stop without another attempt."""

from collections.abc import Sequence

import pytest

from pip._vendor.nab_resolver.errors import (
    ResolutionError,
    ResolutionInvariantError,
    ResolutionLimitError,
    ResolutionStalledError,
)
from pip._vendor.nab_resolver.resolver import Resolver as CoreResolver
from pip._vendor.nab_resolver.resolver import Solution
from pip._vendor.nab_resolver.types import RootRequirement

from pip._internal.exceptions import InstallationError
from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.nab.base import Candidate
from pip._internal.resolution.nab.resolver import Resolver


@pytest.mark.parametrize("requirements", [[], ["probe"]])
def test_real_iteration_limit_is_not_retried(
    resolver: Resolver, monkeypatch: pytest.MonkeyPatch, requirements: list[str]
) -> None:
    solvers = []
    original = CoreResolver.solve

    def bounded(
        solver: CoreResolver[str, Candidate],
        roots: Sequence[RootRequirement[str, Candidate]],
    ) -> Solution[str, Candidate]:
        solver.max_iterations = 0
        solvers.append(solver)
        return original(solver, roots)

    monkeypatch.setattr(CoreResolver, "solve", bounded)
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
        ResolutionLimitError,
        ResolutionStalledError,
        ResolutionInvariantError,
    ],
)
def test_terminal_errors_are_not_retried(
    resolver: Resolver,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[ResolutionError],
) -> None:
    attempts = 0
    original = error_type("analysis interrupted")

    def stop(
        solver: CoreResolver[str, Candidate],
        roots: Sequence[RootRequirement[str, Candidate]],
    ) -> Solution[str, Candidate]:
        nonlocal attempts
        attempts += 1
        raise original

    monkeypatch.setattr(CoreResolver, "solve", stop)
    collected = resolver.factory.collect_root_requirements([])
    with pytest.raises(InstallationError, match="analysis interrupted") as failure:
        resolver._resolve(collected)
    assert attempts == 1
    assert failure.value.__cause__ is original
