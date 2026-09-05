from tests.lib import PipTestEnvironment, create_basic_wheel_for_package
from tests.lib.wheel import make_wheel


def test_direct_url_discovered_after_installed_candidate(
    script: PipTestEnvironment,
) -> None:
    """Read new dependencies from a direct artifact at an installed version."""
    wheel = create_basic_wheel_for_package(script, "dep", "1.0")
    script.pip("install", "--no-index", wheel)

    source = script.scratch_path / "direct"
    source.mkdir()
    replacement = create_basic_wheel_for_package(
        script, "dep", "1.0", depends=["child==1.0"]
    )
    replacement = replacement.rename(source / replacement.name)
    create_basic_wheel_for_package(script, "child", "1.0")
    app = make_wheel(
        name="app",
        version="1.0",
        metadata=(
            "Metadata-Version: 2.1\nName: app\nVersion: 1.0\n"
            f"Requires-Dist: dep @ {replacement.as_uri()}\n"
        ),
    )
    app.save_to(script.scratch_path / "app-1.0-py2.py3-none-any.whl")

    script.pip(
        "install", "--no-index", "--find-links", script.scratch_path, "dep", "app"
    )
    script.assert_installed(app="1.0", dep="1.0", child="1.0")


def test_try_another_file_when_preferred_wheel_has_inconsistent_metadata(
    script: PipTestEnvironment,
) -> None:
    """Try the next file after rejecting inconsistent wheel metadata."""
    create_basic_wheel_for_package(script, "pkg", "1.0")
    preferred = make_wheel(
        name="pkg", version="1.0", metadata_updates={"Version": "9.0"}
    )
    preferred.save_to(script.scratch_path / "pkg-1.0-2-py2.py3-none-any.whl")

    script.pip("install", "--no-index", "--find-links", script.scratch_path, "pkg")
    script.assert_installed(pkg="1.0")
