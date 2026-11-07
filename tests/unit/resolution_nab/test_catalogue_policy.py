"""Apply pip's upgrade and dependency policies at the catalogue boundary."""

from io import BytesIO

import pytest

from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import Version

from pip._internal.metadata import BaseDistribution, MemoryWheel
from pip._internal.metadata.importlib import Distribution
from pip._internal.models.link import Link
from pip._internal.req.constructors import install_req_from_line
from pip._internal.req.req_install import InstallRequirement
from pip._internal.resolution.nab.candidates import LinkCandidate
from pip._internal.resolution.nab.catalogue import PipCatalogue
from pip._internal.resolution.nab.factory import Factory

from tests.lib.wheel import make_wheel


@pytest.mark.parametrize(
    "strategy,requested,package,expected",
    [
        ("eager", [], "probe", True),
        ("only-if-needed", ["probe"], "probe", True),
        ("only-if-needed", ["probe[feature]"], "probe", True),
        ("only-if-needed", ["probe"], "probe[feature]", True),
        ("only-if-needed", ["other"], "probe", False),
        ("to-satisfy-only", ["probe"], "probe", False),
    ],
)
def test_upgrade_policy_uses_requested_base_names(
    factory: Factory, strategy: str, requested: list[str], package: str, expected: bool
) -> None:
    collected = factory.collect_root_requirements(
        [install_req_from_line(item, user_supplied=True) for item in requested]
    )
    catalogue = PipCatalogue(
        factory, collected, ignore_dependencies=False, upgrade_strategy=strategy
    )

    assert catalogue.eligible_for_upgrade(package) is expected


@pytest.mark.parametrize("ignore_dependencies", [False, True])
def test_no_deps_preserves_requires_python(
    factory: Factory, monkeypatch: pytest.MonkeyPatch, ignore_dependencies: bool
) -> None:
    wheel = make_wheel(
        "probe",
        "1",
        metadata_updates={
            "Requires-Dist": ["child>=1"],
            "Requires-Python": ">=3.8",
        },
    )
    distribution = Distribution.from_wheel(
        MemoryWheel("probe-1-py3-none-any.whl", BytesIO(wheel.as_bytes())), "probe"
    )
    link = Link("https://index.invalid/probe-1-py3-none-any.whl")

    # Keep real metadata without downloading the link.
    def prepare(
        requirement: InstallRequirement, **_options: object
    ) -> BaseDistribution:
        assert requirement.link == link
        return distribution

    monkeypatch.setattr(factory.preparer, "prepare_linked_requirement", prepare)
    candidate = LinkCandidate(
        link,
        install_req_from_line("probe"),
        factory,
        name=canonicalize_name("probe"),
        version=Version("1"),
    )
    catalogue = PipCatalogue(
        factory,
        factory.collect_root_requirements([]),
        ignore_dependencies=ignore_dependencies,
        upgrade_strategy="to-satisfy-only",
    )

    names = {requirement.name for requirement in catalogue.get_dependencies(candidate)}

    assert "<Python from Requires-Python>" in names
    assert ("child" in names) is not ignore_dependencies
