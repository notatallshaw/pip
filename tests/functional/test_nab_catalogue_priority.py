from tests.lib import PipTestEnvironment, create_basic_wheel_for_package


def test_catalogue_decides_the_narrow_package_before_a_wide_root(
    script: PipTestEnvironment,
) -> None:
    for version in range(1, 17):
        create_basic_wheel_for_package(script, "wide", str(version))
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
    assert "narrow-1.0" in pins[0]
    assert not any("wide-16" in line for line in pins)
