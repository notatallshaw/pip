from io import BytesIO

import pytest

from pip._vendor.packaging.ranges import VersionRange
from pip._vendor.packaging.version import Version

from pip._internal.metadata import MemoryWheel
from pip._internal.metadata.importlib import Distribution
from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.nab.catalogue import PipCatalogue
from pip._internal.resolution.nab.factory import Factory

from tests.lib.wheel import make_wheel


def installed_catalogue(factory: Factory, requirements: list[str]) -> PipCatalogue:
    """Build a catalogue with probe1 installed and the given inputs."""
    wheel = make_wheel("probe", "1")
    dist = Distribution.from_wheel(
        MemoryWheel("probe-1-py3-none-any.whl", BytesIO(wheel.as_bytes())), "probe"
    )
    factory._installed_dists[dist.canonical_name] = dist
    collected = factory.collect_root_requirements(
        [install_req_from_line(requirement) for requirement in requirements]
    )
    return PipCatalogue(
        factory,
        collected,
        ignore_dependencies=False,
        upgrade_strategy="to-satisfy-only",
    )


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
    requirements: list[str],
    identifier: str,
) -> None:
    catalogue = installed_catalogue(factory, requirements)

    assert catalogue.installed_version(identifier) == Version("1")
    assert Version("1") not in catalogue.input_bounds(identifier)


def test_temporary_solver_exclusion_does_not_change_fixed_input_eligibility(
    factory: Factory,
) -> None:
    catalogue = installed_catalogue(factory, ["probe"])
    assert not catalogue.ordered("probe", VersionRange.singleton(Version("2")))

    assert catalogue.installed_version("probe") == Version("1")
    assert Version("1") in catalogue.input_bounds("probe")
