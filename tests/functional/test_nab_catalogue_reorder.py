from pathlib import Path

from tests.lib import PipTestEnvironment, create_basic_wheel_for_package
from tests.lib.wheel import make_wheel


def blocked_parent_wheels(script: PipTestEnvironment) -> list[Path]:
    """Make a small hub domain whose newest release blocks every parent release."""
    for version in (1, 2, 3):
        create_basic_wheel_for_package(script, "blocker", str(version))
    create_basic_wheel_for_package(script, "hub", "2", depends=["blocker>=2"])
    create_basic_wheel_for_package(script, "hub", "1", depends=["blocker>=1"])
    return [
        create_basic_wheel_for_package(
            script, "parent", str(version), depends=["blocker<2"]
        )
        for version in range(1, 17)
    ]


def test_catalogue_reorders_without_preparing_cached_versions_again(
    script: PipTestEnvironment,
) -> None:
    parents = blocked_parent_wheels(script)
    result = script.pip(
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "parent",
        "hub",
    )

    script.assert_installed(parent="16", hub="1", blocker="1")
    assert result.stdout.count("Nab catalogue reordered") == 1
    assert "Nab catalogue success" in result.stdout
    assert sum(wheel.name in result.stdout for wheel in parents) == 8
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
    parents = blocked_parent_wheels(script)
    result = script.pip(
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "hub",
        "parent",
    )

    script.assert_installed(parent="16", hub="1", blocker="1")
    assert result.stdout.count("Nab catalogue reordered") == 1
    assert "Nab catalogue success" in result.stdout
    assert sum(wheel.name in result.stdout for wheel in parents) == 16
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
        "parent",
        "hub",
        "url-parent",
    )

    script.assert_installed(
        parent="16", hub="1", blocker="1", leaf="1", **{"url-parent": "32"}
    )
    assert result.stdout.index("Nab catalogue reordered") < result.stdout.index(
        "Nab catalogue fallback"
    )
    assert "CatalogueUnsupported: URL dependency" in result.stdout
