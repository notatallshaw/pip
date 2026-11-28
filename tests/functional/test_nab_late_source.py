from tests.lib import PipTestEnvironment, create_basic_wheel_for_package
from tests.lib.wheel import make_wheel


def test_later_url_can_supply_a_version_absent_from_the_index(
    script: PipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(script, "dep", "1.0")
    source = script.scratch_path / "direct"
    source.mkdir()
    direct = create_basic_wheel_for_package(script, "dep", "3.0")
    direct = direct.rename(source / direct.name)
    create_basic_wheel_for_package(script, "app", "2.0", depends=["dep>=2"])
    older_app = make_wheel(
        name="app",
        version="1.0",
        metadata_updates={"Requires-Dist": f"dep @ {direct.as_uri()}"},
    )
    older_app.save_to(script.scratch_path / "app-1.0-py2.py3-none-any.whl")

    script.pip_install_local("app", find_links=script.scratch_path)

    script.assert_installed(app="1.0", dep="3.0")
