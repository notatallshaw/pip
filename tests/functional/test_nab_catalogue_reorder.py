from pathlib import Path

from tests.lib import PipTestEnvironment, TestPipResult, create_basic_wheel_for_package
from tests.lib.wheel import make_wheel

CATALOGUE_PROBE = """\
import runpy
from pip._internal.resolution.nab.catalogue import CatalogueProvider

original = CatalogueProvider._solve_once
def solve(provider, reporter):
    print("CATALOGUE_SOLVE", flush=True)
    return original(provider, reporter)
CatalogueProvider._solve_once = solve
runpy.run_module("pip", run_name="__main__", alter_sys=True)
"""


def tracked_catalogue_install(
    script: PipTestEnvironment, *requirements: str
) -> TestPipResult:
    """Count actual catalogue solver attempts during an offline installation."""
    probe = script.scratch_path / "catalogue_probe.py"
    probe.write_text(CATALOGUE_PROBE)
    return script.run(
        "python",
        str(probe),
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        str(script.scratch_path),
        *requirements,
    )


def blocked_parent_wheels(
    script: PipTestEnvironment,
) -> tuple[list[Path], list[Path]]:
    """Make a hub choice block a transitive dependency of a wider root."""
    for version in (1, 2, 3):
        create_basic_wheel_for_package(script, "blocker", str(version))
    create_basic_wheel_for_package(script, "hub", "2", depends=["blocker>=2"])
    create_basic_wheel_for_package(script, "hub", "1", depends=["blocker>=1"])
    apps = [
        create_basic_wheel_for_package(script, "app", str(version), depends=["parent"])
        for version in range(1, 17)
    ]
    parents = [
        create_basic_wheel_for_package(
            script, "parent", str(version), depends=["blocker<2"]
        )
        for version in range(1, 25)
    ]
    return apps, parents


def test_requested_roots_resolve_without_restarting_the_catalogue(
    script: PipTestEnvironment,
) -> None:
    apps, parents = blocked_parent_wheels(script)
    result = tracked_catalogue_install(script, "app", "hub")

    script.assert_installed(app="16", parent="24", hub="1", blocker="1")
    assert result.stdout.splitlines().count("CATALOGUE_SOLVE") == 1
    assert "Nab catalogue success" in result.stdout
    assert sum(wheel.name in result.stdout for wheel in apps) == 1
    assert sum(wheel.name in result.stdout for wheel in parents) == 24
    assert (
        sum(
            "Processing " in line and parents[-1].name in line
            for line in result.stdout.splitlines()
        )
        == 1
    )


def test_root_priority_preserves_backtracking_when_the_first_root_is_blocking(
    script: PipTestEnvironment,
) -> None:
    apps, parents = blocked_parent_wheels(script)
    result = tracked_catalogue_install(script, "hub", "app")

    script.assert_installed(app="16", parent="24", hub="1", blocker="1")
    assert result.stdout.splitlines().count("CATALOGUE_SOLVE") == 1
    assert "Nab catalogue success" in result.stdout
    assert sum(wheel.name in result.stdout for wheel in apps) > 1
    assert sum(wheel.name in result.stdout for wheel in parents) == 24
    assert (
        sum(
            "Processing " in line and parents[-1].name in line
            for line in result.stdout.splitlines()
        )
        == 1
    )


def test_root_priority_keeps_contextual_fallback_for_a_later_url(
    script: PipTestEnvironment,
) -> None:
    blocked_parent_wheels(script)
    leaf = create_basic_wheel_for_package(script, "leaf", "1")
    hidden = script.scratch_path / "hidden"
    hidden.mkdir()
    leaf = leaf.rename(hidden / leaf.name)
    for version in range(1, 33):
        wheel = make_wheel(
            name="url_parent",
            version=str(version),
            metadata=(
                f"Metadata-Version: 2.1\nName: url-parent\nVersion: {version}\n"
                f"Requires-Dist: leaf @ {leaf.as_uri()}\n"
            ),
        )
        wheel.save_to_dir(script.scratch_path)
    result = tracked_catalogue_install(script, "app", "hub", "url-parent")

    script.assert_installed(
        app="16", parent="24", hub="1", blocker="1", leaf="1", **{"url-parent": "32"}
    )
    assert result.stdout.splitlines().count("CATALOGUE_SOLVE") == 1
    assert "CatalogueUnsupported: URL dependency" in result.stdout


def test_requested_wide_root_precedes_the_narrow_root_and_its_dependencies(
    script: PipTestEnvironment,
) -> None:
    _, parents = blocked_parent_wheels(script)
    result = tracked_catalogue_install(script, "parent", "hub")

    script.assert_installed(parent="24", hub="1", blocker="1")
    assert result.stdout.splitlines().count("CATALOGUE_SOLVE") == 1
    assert "Nab catalogue success" in result.stdout
    assert sum(wheel.name in result.stdout for wheel in parents) == 1
