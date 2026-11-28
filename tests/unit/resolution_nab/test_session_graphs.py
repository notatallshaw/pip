"""Resolve local wheels with pip's finder, metadata and release policy."""

from pathlib import Path
from tempfile import mkdtemp

import pytest

from pip._internal.exceptions import InstallationError
from pip._internal.index.package_finder import PackageFinder
from pip._internal.models.release_control import ReleaseControl
from pip._internal.models.search_scope import SearchScope
from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.nab.catalogue import PipCatalogue
from pip._internal.resolution.nab.factory import Factory

from tests.lib.wheel import make_wheel


def local_catalogue(
    factory: Factory,
    finder: PackageFinder,
    directory: Path,
    graph: dict[str, dict[str, list[str]]],
    roots: list[str],
    constraints: tuple[str, ...] = (),
) -> PipCatalogue:
    # Pip caches local directory listings beyond the parametrized test case.
    directory = Path(mkdtemp(dir=directory))
    for package, releases in graph.items():
        for version, dependencies in releases.items():
            make_wheel(
                package, version, metadata_updates={"Requires-Dist": dependencies}
            ).save_to_dir(directory)

    finder.search_scope = SearchScope([str(directory)], [], True)
    inputs = [install_req_from_line(root) for root in roots]
    inputs.extend(install_req_from_line(item, constraint=True) for item in constraints)
    return PipCatalogue(
        factory,
        factory.collect_root_requirements(inputs),
        ignore_dependencies=False,
        upgrade_strategy="to-satisfy-only",
    )


def test_selected_graph_retains_the_finder_files(
    factory: Factory, finder: PackageFinder, tmp_path: Path
) -> None:
    catalogue = local_catalogue(
        factory,
        finder,
        tmp_path,
        {"app": {"2": ["dep==2"], "1": ["dep==1"]}, "dep": {"1": []}},
        ["app"],
    )
    solution = catalogue.solve(try_live=True)

    assert solution.edges == (("app", "dep"),)
    for name, choice in solution.candidates.items():
        link = catalogue.selected_data(choice).native.source_link
        assert link is not None
        assert link.file_path == str(next(tmp_path.rglob(f"{name}-1-*.whl")))


@pytest.mark.parametrize(
    "policy, broken_final, expected",
    [
        ("default", True, None),
        ("final", True, None),
        ("pre", True, "2rc1"),
        ("pre", False, "2"),
    ],
)
def test_prerelease_after_matching_final_fails(
    factory: Factory,
    finder: PackageFinder,
    tmp_path: Path,
    policy: str,
    broken_final: bool,
    expected: str | None,
) -> None:
    if policy == "pre":
        finder.set_release_control(ReleaseControl(all_releases={":all:"}))
    elif policy == "final":
        finder.set_release_control(ReleaseControl(only_final={"dep"}))

    dependencies = ["missing==1"] if broken_final else []
    catalogue = local_catalogue(
        factory,
        finder,
        tmp_path,
        {"dep": {"1": [], "2": dependencies, "2.0": dependencies, "2rc1": []}},
        ["dep>1"],
    )

    if expected is None:
        with pytest.raises(InstallationError):
            catalogue.solve(try_live=True)
    else:
        assert catalogue.solve(try_live=True).candidates["dep"].label == expected


@pytest.mark.parametrize(
    "graph, roots, constraints, expected",
    [
        pytest.param(
            {
                "app": {"1": ["dep==1"], "2": ["dep==2rc1"]},
                "dep": {"1": [], "2rc1": []},
            },
            ["app"],
            (),
            {"app": "2", "dep": "2rc1"},
            id="exact-prerelease",
        ),
        pytest.param(
            {"app": {"1": ["dep==1"], "2": ["dep<1"]}, "dep": {"1": [], "0.9b1": []}},
            ["app"],
            (),
            {"app": "2", "dep": "0.9b1"},
            id="no-final-in-range",
        ),
        pytest.param(
            {"dep": {"2": ["missing"], "2rc1": []}},
            ["dep>1"],
            ("dep!=2",),
            {"dep": "2rc1"},
            id="original-constraint",
        ),
        pytest.param(
            {
                "app": {"2": ["bridge"]},
                "bridge": {"1": ["dep>=2rc1"]},
                "dep": {"2": ["missing"], "2rc1": []},
            },
            ["dep>1", "app"],
            (),
            {"app": "2", "bridge": "1", "dep": "2rc1"},
            id="transitive-prerelease",
        ),
        pytest.param(
            {
                "app": {"2": ["dep>1"], "1": ["dep!=2"]},
                "dep": {"2": ["missing"], "2rc1": []},
            },
            ["app", "dep>1"],
            (),
            {"app": "1", "dep": "2rc1"},
            id="backtracked-parent",
        ),
        pytest.param(
            {
                "app": {"2": ["dep>=2rc1", "missing"], "1": ["dep>1"]},
                "dep": {"2": ["missing"], "2rc1": []},
            },
            ["app"],
            (),
            None,
            id="rejected-parent",
        ),
        pytest.param(
            {
                "app": {"1": ["shared==2"], "2": ["shared==1"]},
                "dep": {"2": ["shared==2"], "2rc1": ["shared==1"]},
                "shared": {"1": [], "2": []},
            },
            ["app", "dep>1"],
            (),
            {"app": "1", "dep": "2", "shared": "2"},
            id="keep-final",
        ),
    ],
)
def test_selected_declarations_control_prerelease_admission(
    factory: Factory,
    finder: PackageFinder,
    tmp_path: Path,
    graph: dict[str, dict[str, list[str]]],
    roots: list[str],
    constraints: tuple[str, ...],
    expected: dict[str, str] | None,
) -> None:
    catalogue = local_catalogue(factory, finder, tmp_path, graph, roots, constraints)

    if expected is None:
        with pytest.raises(InstallationError):
            catalogue.solve(try_live=True)
    else:
        selected = catalogue.solve(try_live=True).candidates
        assert {
            name: candidate.label for name, candidate in selected.items()
        } == expected
