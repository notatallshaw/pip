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


def test_catalogue_revisits_a_blocker_before_reading_every_parent(
    script: PipTestEnvironment,
) -> None:
    for version in (1, 2):
        create_basic_wheel_for_package(script, "blocker", str(version))
    parents = [
        create_basic_wheel_for_package(
            script, "parent", str(version), depends=["blocker==1"]
        )
        for version in range(1, 17)
    ]

    result = script.pip(
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "blocker",
        "parent",
    )

    script.assert_installed(parent="16", blocker="1")
    assert "Nab catalogue success" in result.stdout
    prepared = [wheel for wheel in parents if wheel.name in result.stdout]
    assert 1 < len(prepared) <= 4
