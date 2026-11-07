"""Original input bounds retain eligible files across native candidate queries."""

from pathlib import Path

import pytest

from pip._internal.index.package_finder import PackageFinder
from pip._internal.models.candidate import InstallationCandidate
from pip._internal.models.link import Link
from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.nab.factory import Factory
from pip._internal.resolution.nab.host import PipHost
from pip._internal.resolution.nab.resolver import Resolver

from tests.lib.wheel import make_wheel


def artifacts_in(directory: Path, *, yanked: bool) -> list[InstallationCandidate]:
    """Build distinct native files with an excluded older numeric group."""
    artifacts = []
    for version in ("1", "2", "1.0"):
        path = Path(
            make_wheel(
                "probe", version, metadata_updates={"Provides-Extra": ["opt", "a", "b"]}
            ).save_to_dir(directory)
        )
        artifacts.append(
            InstallationCandidate(
                "probe",
                version,
                Link(
                    path.as_uri(),
                    yanked_reason="withdrawn" if yanked and version != "2" else None,
                ),
            )
        )
    return artifacts


@pytest.mark.parametrize("yanked", [False, True])
@pytest.mark.parametrize(
    "requirements,constraints",
    [
        (["probe>=2"], []),
        (["probe", "probe>=2"], []),
        (["probe[opt]>=2"], []),
        (["probe[opt]", "probe>=2"], []),
        (["probe[a]>=2", "probe[b]"], []),
        (["probe"], ["probe>=2"]),
        (["probe[opt]"], ["probe>=2"]),
    ],
)
def test_excluded_groups_do_not_change_the_provider(
    factory: Factory,
    finder: PackageFinder,
    resolver: Resolver,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    requirements: list[str],
    constraints: list[str],
    *,
    yanked: bool,
) -> None:
    artifacts = artifacts_in(tmp_path, yanked=yanked)
    monkeypatch.setattr(finder, "find_all_candidates", lambda _name: artifacts)
    inputs = [install_req_from_line(item) for item in requirements]
    inputs.extend(install_req_from_line(item, constraint=True) for item in constraints)
    result = resolver._resolve(factory.collect_root_requirements(inputs))
    assert result.mapping
    assert all(str(candidate.version) == "2" for candidate in result.mapping.values())
    assert all(
        candidate.source_link == artifacts[1].link
        for candidate in result.mapping.values()
    )


@pytest.mark.parametrize("yanked", [False, True])
def test_temporary_solver_bounds_cannot_hide_an_input_eligible_group(
    factory: Factory,
    finder: PackageFinder,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    yanked: bool,
) -> None:
    artifacts = artifacts_in(tmp_path, yanked=yanked)
    monkeypatch.setattr(finder, "find_all_candidates", lambda _name: artifacts)
    collected = factory.collect_root_requirements([install_req_from_line("probe==1")])
    host = PipHost(
        factory,
        collected,
        ignore_dependencies=False,
        upgrade_strategy="to-satisfy-only",
    )
    declarations = {"probe": collected.requirements}
    first = next(iter(host.find_matches("probe", declarations, {})))
    next_query = list(host.find_matches("probe", declarations, {"probe": [first]}))
    assert all(candidate != first for candidate in next_query)
    assert next(iter(host.find_matches("probe", declarations, {}))) == first
