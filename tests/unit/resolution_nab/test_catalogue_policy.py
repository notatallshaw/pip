"""Apply pip's upgrade and dependency policies at the catalogue boundary."""

from io import BytesIO
from pathlib import Path

import pytest

from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import Version

from pip._internal.index.package_finder import PackageFinder
from pip._internal.metadata import BaseDistribution, MemoryWheel
from pip._internal.metadata.importlib import Distribution
from pip._internal.models.link import Link
from pip._internal.models.search_scope import SearchScope
from pip._internal.req.constructors import install_req_from_line
from pip._internal.req.req_install import InstallRequirement
from pip._internal.resolution.nab.candidates import LinkCandidate
from pip._internal.resolution.nab.factory import Factory
from pip._internal.resolution.nab.host import PipHost

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
    factory: Factory,
    finder: PackageFinder,
    tmp_path: Path,
    strategy: str,
    requested: list[str],
    package: str,
    expected: bool,
) -> None:
    installed = make_wheel("probe", "1").as_distribution("probe")
    factory._installed_dists[canonicalize_name("probe")] = installed
    make_wheel("probe", "2").save_to_dir(tmp_path)
    finder.search_scope = SearchScope([str(tmp_path)], [], False)
    collected = factory.collect_root_requirements(
        [install_req_from_line(item, user_supplied=True) for item in requested]
    )
    host = PipHost(
        factory, collected, ignore_dependencies=False, upgrade_strategy=strategy
    )
    requirement = list(factory.make_requirements_from_spec(package, None))[0]
    choices = host.find_matches(package, {package: [requirement]}, {})
    assert str(next(iter(choices)).version) == ("2" if expected else "1")


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
    host = PipHost(
        factory,
        factory.collect_root_requirements([]),
        ignore_dependencies=ignore_dependencies,
        upgrade_strategy="to-satisfy-only",
    )

    names = {requirement.name for requirement in host.get_dependencies(candidate)}

    assert "<Python from Requires-Python>" in names
    assert ("child" in names) is not ignore_dependencies
