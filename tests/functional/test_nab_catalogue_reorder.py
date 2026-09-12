from pathlib import Path

from tests.lib import PipTestEnvironment, create_basic_wheel_for_package
from tests.lib.wheel import make_wheel


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


def test_catalogue_reorders_without_preparing_cached_versions_again(
    script: PipTestEnvironment,
) -> None:
    apps, parents = blocked_parent_wheels(script)
    result = script.pip(
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "app",
        "hub",
    )

    script.assert_installed(app="16", parent="24", hub="1", blocker="1")
    assert result.stdout.count("Nab catalogue reordered") == 1
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


def test_catalogue_reorders_only_once_when_the_second_order_also_backtracks(
    script: PipTestEnvironment,
) -> None:
    apps, parents = blocked_parent_wheels(script)
    result = script.pip(
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "hub",
        "app",
    )

    script.assert_installed(app="16", parent="24", hub="1", blocker="1")
    assert result.stdout.count("Nab catalogue reordered") == 1
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


def test_catalogue_still_falls_back_for_a_url_discovered_after_reordering(
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
    result = script.pip(
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "app",
        "hub",
        "url-parent",
    )

    script.assert_installed(
        app="16", parent="24", hub="1", blocker="1", leaf="1", **{"url-parent": "32"}
    )
    assert result.stdout.index("Nab catalogue reordered") < result.stdout.index(
        "Nab catalogue fallback"
    )
    assert "CatalogueUnsupported: URL dependency" in result.stdout


def test_catalogue_keeps_size_first_order_while_scanning_a_requested_root(
    script: PipTestEnvironment,
) -> None:
    _, parents = blocked_parent_wheels(script)
    result = script.pip(
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "parent",
        "hub",
    )

    script.assert_installed(parent="24", hub="1", blocker="1")
    assert "Nab catalogue reordered" not in result.stdout
    assert "Nab catalogue success" in result.stdout
    assert sum(wheel.name in result.stdout for wheel in parents) == 24
