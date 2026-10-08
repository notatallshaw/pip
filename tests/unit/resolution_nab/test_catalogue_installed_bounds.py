from io import BytesIO

import pytest

from pip._vendor.packaging.ranges import VersionRange
from pip._vendor.packaging.version import Version

from pip._internal.metadata import MemoryWheel
from pip._internal.metadata.importlib import Distribution
from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.nab.catalogue import CatalogueProvider
from pip._internal.resolution.nab.factory import Factory
from pip._internal.resolution.nab.provider import PipProvider

from tests.lib.wheel import make_wheel


def installed_catalogue(
    factory: Factory, provider: PipProvider, requirements: list[str]
) -> CatalogueProvider:
    """Build a catalogue with probe1 installed and the given inputs."""
    wheel = make_wheel("probe", "1")
    dist = Distribution.from_wheel(
        MemoryWheel("probe-1-py3-none-any.whl", BytesIO(wheel.as_bytes())), "probe"
    )
    factory._installed_dists[dist.canonical_name] = dist
    collected = factory.collect_root_requirements(
        [install_req_from_line(requirement) for requirement in requirements]
    )
    return CatalogueProvider(factory, provider, collected)


@pytest.mark.parametrize(
    "requirements,identifier",
    [
        (["probe[feature]", "probe>=2"], "probe[feature]"),
        (["probe[feature]>=2"], "probe"),
        (["probe[a]>=2", "probe[b]"], "probe[b]"),
    ],
)
def test_fixed_base_bounds_exclude_installed_versions_for_every_identifier(
    factory: Factory,
    provider: PipProvider,
    requirements: list[str],
    identifier: str,
) -> None:
    catalogue = installed_catalogue(factory, provider, requirements)

    assert catalogue.installed_version(identifier) == Version("1")
    assert not catalogue.installed_version_matches_inputs(identifier)


def test_temporary_solver_exclusion_does_not_change_fixed_input_eligibility(
    factory: Factory, provider: PipProvider
) -> None:
    catalogue = installed_catalogue(factory, provider, ["probe"])
    catalogue.receive_partial_solution_hint(
        {"probe": VersionRange.singleton(Version("2"))}, {}
    )

    assert catalogue.installed_version("probe") == Version("1")
    assert catalogue.installed_version_matches_inputs("probe")
