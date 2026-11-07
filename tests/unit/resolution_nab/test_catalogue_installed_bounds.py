"""Apply original base bounds to installed candidates and every extras view."""

from pathlib import Path

import pytest

from pip._vendor.packaging.utils import canonicalize_name

from pip._internal.index.package_finder import PackageFinder
from pip._internal.models.search_scope import SearchScope
from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.nab.factory import Factory
from pip._internal.resolution.nab.host import PipHost
from pip._internal.resolution.nab.resolver import Resolver

from tests.lib.wheel import make_wheel


@pytest.mark.parametrize(
    "requirements",
    [
        ["probe[feature]", "probe>=2"],
        ["probe[feature]>=2"],
        ["probe[a]>=2", "probe[b]"],
    ],
)
def test_fixed_base_bounds_exclude_installed_versions_for_every_identifier(
    factory: Factory,
    finder: PackageFinder,
    resolver: Resolver,
    tmp_path: Path,
    requirements: list[str],
) -> None:
    dist = make_wheel("probe", "1").as_distribution("probe")
    factory._installed_dists[canonicalize_name("probe")] = dist
    make_wheel(
        "probe", "2", metadata_updates={"Provides-Extra": ["feature", "a", "b"]}
    ).save_to_dir(tmp_path)
    finder.search_scope = SearchScope([str(tmp_path)], [], False)
    collected = factory.collect_root_requirements(
        [install_req_from_line(item) for item in requirements]
    )
    selected = resolver._resolve(collected).mapping
    assert "probe" in selected
    assert all(str(candidate.version) == "2" for candidate in selected.values())
    assert any(name != "probe" for name in selected)


def test_temporary_solver_exclusion_does_not_change_fixed_input_eligibility(
    factory: Factory,
) -> None:
    dist = make_wheel("probe", "1").as_distribution("probe")
    factory._installed_dists[canonicalize_name("probe")] = dist
    collected = factory.collect_root_requirements([install_req_from_line("probe")])
    host = PipHost(
        factory,
        collected,
        ignore_dependencies=False,
        upgrade_strategy="to-satisfy-only",
    )
    declarations = {"probe": collected.requirements}
    (incumbent,) = host.find_matches("probe", declarations, {})
    assert incumbent.is_installed
    assert not list(host.find_matches("probe", declarations, {"probe": [incumbent]}))
    assert list(host.find_matches("probe", declarations, {})) == [incumbent]
