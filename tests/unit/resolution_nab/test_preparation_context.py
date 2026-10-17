"""Retain metadata from separate native preparation contexts."""

from io import BytesIO

import pytest

from pip._vendor.nab_resolver.candidate_provider import PreparedCandidate
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import Version

from pip._internal.metadata import BaseDistribution, MemoryWheel
from pip._internal.metadata.importlib import Distribution
from pip._internal.models.link import Link
from pip._internal.req.constructors import install_req_from_line
from pip._internal.req.req_install import InstallRequirement
from pip._internal.resolution.nab.candidates import LinkCandidate
from pip._internal.resolution.nab.factory import Factory
from pip._internal.resolution.nab.host import NativeHost
from pip._internal.resolution.nab.provider import PipProvider
from pip._internal.resolution.nab.ranges import CandidateKey

from tests.lib.wheel import make_wheel


def test_direct_metadata_does_not_reuse_a_configured_index_dependency_list(
    factory: Factory, provider: PipProvider, monkeypatch: pytest.MonkeyPatch
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
    host = NativeHost(factory, provider)
    observed = []
    for candidate in (ordinary, direct):
        prepared = PreparedCandidate(
            CandidateKey(candidate.version, host._source(candidate)), candidate
        )
        observed.append(
            {requirement.package for requirement in host.get_dependencies(prepared)}
        )

    assert observed == [{"alpha"}, {"beta"}]
