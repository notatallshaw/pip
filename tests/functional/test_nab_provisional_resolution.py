from tests.lib import PipTestEnvironment, create_basic_wheel_for_package
from tests.lib.wheel import make_wheel


def test_late_source_retry_preserves_satisfied_installed_dependencies(
    script: PipTestEnvironment,
) -> None:
    installed = create_basic_wheel_for_package(script, "leaf", "1.0")
    script.pip("install", "--no-index", installed)
    create_basic_wheel_for_package(script, "leaf", "2.0")

    create_basic_wheel_for_package(script, "dep", "1.0")
    source = script.scratch_path / "direct"
    source.mkdir()
    direct = create_basic_wheel_for_package(script, "dep", "3.0")
    direct = direct.rename(source / direct.name)
    create_basic_wheel_for_package(script, "app", "2.0", depends=["dep>=2", "leaf>=1"])
    make_wheel(
        name="app",
        version="1.0",
        metadata=(
            "Metadata-Version: 2.1\nName: app\nVersion: 1.0\n"
            f"Requires-Dist: dep @ {direct.as_uri()}\n"
            "Requires-Dist: leaf>=1\n"
        ),
    ).save_to(script.scratch_path / "app-1.0-py2.py3-none-any.whl")

    script.pip("install", "--no-index", "--find-links", script.scratch_path, "app")

    script.assert_installed(app="1.0", dep="3.0", leaf="1.0")
