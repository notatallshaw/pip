from tests.lib import PipTestEnvironment, create_basic_wheel_for_package


def test_catalogue_rejects_a_dependency_conflict_before_pinning(
    script: PipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(script, "leaf", "1.0")
    create_basic_wheel_for_package(script, "chooser", "2.0", depends=["leaf>=2"])
    create_basic_wheel_for_package(script, "chooser", "1.0", depends=["leaf==1"])
    script.environ["PIP_RESOLVER_DEBUG"] = "1"

    result = script.pip(
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "leaf==1",
        "chooser",
    )

    script.assert_installed(leaf="1.0", chooser="1.0")
    pins = [line for line in result.stdout.splitlines() if "Reporter.pinning(" in line]
    assert any("chooser-1.0" in line for line in pins)
    assert not any("chooser-2.0" in line for line in pins)
