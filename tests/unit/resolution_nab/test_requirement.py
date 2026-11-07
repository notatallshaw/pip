import os
from pathlib import Path

import pytest

from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.nab.base import Candidate, Requirement
from pip._internal.resolution.nab.factory import Factory
from pip._internal.resolution.nab.host import PipHost
from pip._internal.resolution.nab.resolver import Resolver

from tests.lib import TestData


def candidates_for(factory: Factory, spec: str) -> tuple[Requirement, list[Candidate]]:
    collected = factory.collect_root_requirements([install_req_from_line(spec)])
    (requirement,) = collected.requirements
    host = PipHost(
        factory,
        collected,
        ignore_dependencies=False,
        upgrade_strategy="to-satisfy-only",
    )
    candidates = list(
        host.find_matches(requirement.name, {requirement.name: [requirement]}, {})
    )
    return requirement, candidates


@pytest.fixture
def test_cases(data: TestData) -> list[tuple[str, str, int]]:
    def _data_file(name: str) -> Path:
        return data.packages.joinpath(name)

    def data_file(name: str) -> str:
        return os.fspath(_data_file(name))

    def data_url(name: str) -> str:
        return _data_file(name).as_uri()

    test_cases = [
        # requirement, name, matches
        # Version specifiers
        ("simple", "simple", 3),
        ("simple>1.0", "simple", 2),
        # ("simple[extra]==1.0", "simple[extra]", 1),
        # Wheels
        (data_file("simplewheel-1.0-py2.py3-none-any.whl"), "simplewheel", 1),
        (data_url("simplewheel-1.0-py2.py3-none-any.whl"), "simplewheel", 1),
        # Direct URLs
        # TODO: The following test fails
        # ("foo @ " + data_url("simple-1.0.tar.gz"), "foo", 1),
        # SDists
        # TODO: sdists should have a name
        (data_file("simple-1.0.tar.gz"), "simple", 1),
        (data_url("simple-1.0.tar.gz"), "simple", 1),
        # TODO: directory, editables
    ]

    return test_cases


def test_new_resolver_requirement_has_name(
    test_cases: list[tuple[str, str, int]], factory: Factory
) -> None:
    """All requirements should have a name"""
    for spec, name, _ in test_cases:
        reqs = list(factory.make_requirements_from_spec(spec, comes_from=None))
        assert len(reqs) == 1
        assert reqs[0].name == name


def test_new_resolver_correct_number_of_matches(
    test_cases: list[tuple[str, str, int]], factory: Factory
) -> None:
    """Requirements should return the correct number of candidates"""
    for spec, _, match_count in test_cases:
        _, candidates = candidates_for(factory, spec)
        assert len(candidates) == match_count


def test_new_resolver_candidates_match_requirement(
    test_cases: list[tuple[str, str, int]], factory: Factory
) -> None:
    for spec, _, _ in test_cases:
        requirement, candidates = candidates_for(factory, spec)
        for candidate in candidates:
            assert isinstance(candidate, Candidate)
            assert requirement.is_satisfied_by(candidate)


def test_new_resolver_full_resolve(factory: Factory, resolver: Resolver) -> None:
    collected = factory.collect_root_requirements(
        [install_req_from_line("simplewheel")]
    )
    result = resolver._resolve(collected)
    assert set(result.mapping) == {"simplewheel"}
    assert str(result.mapping["simplewheel"].version) == "2.0"
