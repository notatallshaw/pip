from pathlib import Path

from tests.lib import PipTestEnvironment, create_basic_wheel_for_package


def feature_wheel(
    script: PipTestEnvironment, version: int, dependency: str = ""
) -> Path:
    """Create a version whose optional dependency differs from its base metadata."""
    return create_basic_wheel_for_package(
        script,
        "dep",
        f"{version}.0",
        extras={"feature": [dependency] if dependency else []},
    )


def test_extras_skip_metadata_for_versions_excluded_by_the_base(
    script: PipTestEnvironment,
) -> None:
    wheels = [feature_wheel(script, version) for version in range(1, 17)]

    result = script.pip(
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "dep==1",
        "dep[feature]",
    )

    script.assert_installed(dep="1.0")
    assert "Nab catalogue success" in result.stdout
    assert wheels[-1].name not in result.stdout


def test_extras_coupling_reopens_versions_after_a_base_backtrack(
    script: PipTestEnvironment,
) -> None:
    rejected = create_basic_wheel_for_package(
        script, "chooser", "2.0", depends=["dep==1"]
    )
    create_basic_wheel_for_package(script, "chooser", "1.0", depends=["dep==2"])
    create_basic_wheel_for_package(script, "leaf", "1.0")
    for version in range(1, 7):
        feature_wheel(script, version, "leaf==1" if version == 2 else "missing")

    result = script.pip(
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "chooser",
        "dep[feature]",
    )

    script.assert_installed(chooser="1.0", dep="2.0", leaf="1.0")
    assert rejected.name in result.stdout
    assert "Nab catalogue success" in result.stdout
