from pathlib import Path

import pytest

from tests.lib import (
    PipTestEnvironment,
    create_basic_sdist_for_package,
    create_basic_wheel_for_package,
)
from tests.lib.resolver import preparation_count, tracked_install
from tests.lib.wheel import make_wheel


def late_url_wheels(script: PipTestEnvironment) -> tuple[Path, Path]:
    """Make an older app release supply the dependency missing from the index."""
    create_basic_wheel_for_package(script, "dep", "1.0")
    hidden = script.scratch_path / "hidden"
    hidden.mkdir()
    direct = create_basic_wheel_for_package(script, "dep", "3.0")
    direct = direct.rename(hidden / direct.name)
    newer = create_basic_wheel_for_package(script, "app", "2.0", depends=["dep>=2"])
    older = make_wheel(
        name="app",
        version="1.0",
        metadata_updates={"Requires-Dist": f"dep @ {direct.as_uri()}"},
    )
    return Path(older.save_to_dir(script.scratch_path)), newer


def test_fallback_reuses_prepared_wheels_and_accepts_url_dependencies(
    script: PipTestEnvironment,
) -> None:
    older, newer = late_url_wheels(script)
    result = tracked_install(script, "app")

    script.assert_installed(app="1.0", dep="3.0")
    assert "Nab source-domain restart" in result.stdout
    assert preparation_count(result.stdout, newer) == 1
    assert preparation_count(result.stdout, older) == 1


def test_unsatisfiable_catalogue_does_not_retry(
    script: PipTestEnvironment,
) -> None:
    app = create_basic_wheel_for_package(script, "app", "1.0", depends=["dep>=2"])
    create_basic_wheel_for_package(script, "dep", "1.0")
    result = tracked_install(script, "app", expect_error=True)

    assert "Nab source-domain restart" not in result.stdout
    assert "No matching distribution found for dep>=2" in result.stdout + result.stderr
    assert preparation_count(result.stdout, app) == 1


@pytest.mark.parametrize("group", ["numeric", "aliases", "yanked"])
def test_unread_url_does_not_retry_after_static_failure(
    script: PipTestEnvironment, group: str
) -> None:
    create_basic_wheel_for_package(script, "dep", "1.0")
    hidden = script.scratch_path / "hidden"
    hidden.mkdir()
    direct = create_basic_wheel_for_package(script, "dep", "3.0")
    direct = direct.rename(hidden / direct.name)
    make_wheel(
        name="bridge",
        version="1.0",
        metadata_updates={"Requires-Dist": f"dep @ {direct.as_uri()}"},
    ).save_to_dir(script.scratch_path)
    options: tuple[str, ...] = ()
    if group == "aliases":
        create_basic_wheel_for_package(
            script, "bridge", "1.0.0", depends=[f"dep @ {direct.as_uri()}"]
        )
    elif group == "yanked":
        withdrawn = create_basic_wheel_for_package(script, "bridge", "2.0")
        files = script.scratch_path / "withdrawn"
        files.mkdir()
        withdrawn = withdrawn.rename(files / withdrawn.name)
        listing = script.scratch_path / "links.html"
        listing.write_text(
            f'<a href="{withdrawn.as_uri()}" data-yanked="withdrawn">'
            f"{withdrawn.name}</a>"
        )
        options = ("--find-links", str(listing))

    result = tracked_install(
        script, "dep>=2", "bridge", options=options, expect_error=True
    )

    script.assert_not_installed("dep", "bridge")
    assert "Nab source-domain restart" not in result.stdout
    assert "PREPARE bridge-1.0-" not in result.stdout


def test_catalogue_rejects_an_artifact_without_repreparing_it(
    script: PipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(script, "pkg", "1.0")
    create_basic_sdist_for_package(script, "pkg", "2.0")
    rejected = script.scratch_path / "pkg-2.0-py2.py3-none-any.whl"
    make_wheel(name="pkg", version="2.0", metadata_updates={"Version": "9.0"}).save_to(
        rejected
    )
    result = tracked_install(
        script, "pkg", options=("--prefer-binary", "--no-build-isolation")
    )

    script.assert_installed(pkg="1.0")
    assert preparation_count(result.stdout, rejected) == 1


def test_explicit_root_url_discovery_reuses_prepared_metadata(
    script: PipTestEnvironment,
) -> None:
    older, _ = late_url_wheels(script)
    result = tracked_install(script, older)

    script.assert_installed(app="1.0", dep="3.0")
    assert preparation_count(result.stdout, older) == 1
    assert result.stdout.count("Nab source-domain restart:") == 1
    script.pip("check")


def test_backtracking_reads_the_preferred_requested_wheel_once(
    script: PipTestEnvironment,
) -> None:
    for version in (1, 2, 3):
        create_basic_wheel_for_package(script, "blocker", str(version))
    create_basic_wheel_for_package(script, "hub", "2", depends=["blocker>=2"])
    create_basic_wheel_for_package(script, "hub", "1", depends=["blocker>=1"])
    apps = [
        create_basic_wheel_for_package(script, "app", str(version), depends=["parent"])
        for version in range(1, 5)
    ]
    for version in range(1, 6):
        create_basic_wheel_for_package(
            script, "parent", str(version), depends=["blocker<2"]
        )

    result = tracked_install(script, "app", "hub")

    script.assert_installed(app="4", parent="5", hub="1", blocker="1")
    assert preparation_count(result.stdout, apps[-1]) == 1
    assert all(preparation_count(result.stdout, app) == 0 for app in apps[:-1])
