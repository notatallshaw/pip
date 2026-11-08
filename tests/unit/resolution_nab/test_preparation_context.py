"""Retain metadata from separate native preparation contexts."""

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


def test_direct_metadata_does_not_reuse_a_configured_index_dependency_list(
    factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    distributions: dict[str, BaseDistribution] = {}
    for name in ("alpha", "beta"):
        wheel = make_wheel(
            "probe", "1", metadata_updates={"Requires-Dist": [f"{name}>=1"]}
        )
        distributions[name] = Distribution.from_wheel(
            MemoryWheel("probe-1-py3-none-any.whl", BytesIO(wheel.as_bytes())), "probe"
        )

    def prepare(
        requirement: InstallRequirement, *, parallel_builds: bool
    ) -> BaseDistribution:
        """Model a backend whose default and configured metadata differ."""
        variant = "alpha" if bool(requirement.config_settings) else "beta"
        return distributions[variant]

    monkeypatch.setattr(factory.preparer, "prepare_linked_requirement", prepare)
    link = Link("https://index.invalid/probe-1.tar.gz")
    ordinary = LinkCandidate(
        link,
        install_req_from_line("probe", config_settings={"variant": "alpha"}),
        factory,
        name=canonicalize_name("probe"),
        version=Version("1"),
    )
    direct = LinkCandidate(
        link,
        install_req_from_line(f"probe @ {link.url}"),
        factory,
        name=canonicalize_name("probe"),
    )
    collected = factory.collect_root_requirements([])
    catalogue = PipCatalogue(
        factory,
        collected,
        ignore_dependencies=False,
        upgrade_strategy="to-satisfy-only",
    )
    observed = []
    for candidate in (ordinary, direct):
        data = catalogue.read_candidate(catalogue.source_candidate("probe", candidate))
        assert data is not None
        observed.append(set(data.dependencies))

    assert observed == [{"alpha"}, {"beta"}]
