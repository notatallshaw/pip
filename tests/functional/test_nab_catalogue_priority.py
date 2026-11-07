from tests.lib import PipTestEnvironment, create_basic_wheel_for_package


def test_new_parent_bound_skips_middle_versions_of_a_wide_root(
    script: PipTestEnvironment,
) -> None:
    wheels = [
        create_basic_wheel_for_package(script, "wide", str(version))
        for version in range(1, 17)
    ]
    create_basic_wheel_for_package(script, "narrow", "1.0", depends=["wide==1"])
    script.environ["PIP_RESOLVER_DEBUG"] = "1"

    result = script.pip(
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "wide",
        "narrow",
    )

    script.assert_installed(wide="1", narrow="1.0")
    pins = [line for line in result.stdout.splitlines() if "Reporter.pinning(" in line]
    assert any("narrow-1.0" in line for line in pins)
    assert wheels[0].name in pins[-1]
    # One initial wide choice and the two final choices.
    assert len(pins) <= 3
    assert not any(wheel.name in line for wheel in wheels[1:-1] for line in pins)
