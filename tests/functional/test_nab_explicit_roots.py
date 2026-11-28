import pytest

from tests.lib import (
    PipTestEnvironment,
    create_basic_wheel_for_package,
)


def test_explicit_root_supplies_transitive_extras(script: PipTestEnvironment) -> None:
    wheel = create_basic_wheel_for_package(
        script, "app", "1", extras={"feature": ["child==1"]}
    )
    sources = script.scratch_path / "sources"
    sources.mkdir()
    wheel = wheel.rename(sources / wheel.name)
    create_basic_wheel_for_package(script, "app", "2", depends=["missing==1"])
    create_basic_wheel_for_package(script, "bridge", "1", depends=["app[feature]"])
    create_basic_wheel_for_package(script, "child", "1")

    script.pip_install_local(
        wheel, "bridge", find_links=script.scratch_path, build_isolation=True
    )

    script.assert_installed(app="1", bridge="1", child="1")


def test_explicit_root_takes_precedence_over_installed_metadata(
    script: PipTestEnvironment,
) -> None:
    initial = create_basic_wheel_for_package(script, "app", "1", depends=["missing==1"])
    script.pip("install", "--no-index", "--no-deps", initial)
    wheel = create_basic_wheel_for_package(script, "app", "1", depends=["child==1"])
    create_basic_wheel_for_package(script, "child", "1")

    script.pip_install_local(
        "--force-reinstall", wheel, find_links=script.scratch_path, build_isolation=True
    )

    script.assert_installed(app="1", child="1")
    script.assert_not_installed("missing")


@pytest.mark.parametrize("direct_first", [False, True])
@pytest.mark.parametrize("specifier", [">=1", ">=2"])
def test_explicit_root_obeys_named_requirement(
    script: PipTestEnvironment, direct_first: bool, specifier: str
) -> None:
    wheel = create_basic_wheel_for_package(
        script, "app", "1", extras={"feature": ["child==1"]}
    )
    create_basic_wheel_for_package(script, "app", "2")
    create_basic_wheel_for_package(script, "child", "1")
    requirements = [f"app[feature] @ {wheel.as_uri()}", f"app{specifier}"]
    if not direct_first:
        requirements.reverse()

    result = script.pip_install_local(
        *requirements,
        find_links=script.scratch_path,
        build_isolation=True,
        expect_error=specifier == ">=2",
    )

    if specifier == ">=2":
        assert "ResolutionImpossible" in result.stderr
        script.assert_not_installed("app", "child")
    else:
        script.assert_installed(app="1", child="1")
