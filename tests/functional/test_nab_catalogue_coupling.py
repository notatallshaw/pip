from tests.lib import PipTestEnvironment, create_basic_wheel_for_package
from tests.lib.wheel import make_wheel


def feature_wheel(script: PipTestEnvironment, version: int, dependency: str = "") -> None:
    """Create a version whose optional dependency differs from its base metadata."""
    metadata = (
        f"Metadata-Version: 2.1\nName: dep\nVersion: {version}.0\n"
        "Provides-Extra: feature\n"
    )
    if dependency:
        metadata += f'Requires-Dist: {dependency}; extra == "feature"\n'
    wheel = make_wheel(name="dep", version=f"{version}.0", metadata=metadata)
    wheel.save_to(script.scratch_path / f"dep-{version}.0-py3-none-any.whl")


def test_extras_skip_metadata_for_versions_excluded_by_the_base(
    script: PipTestEnvironment,
) -> None:
    for version in range(1, 17):
        feature_wheel(script, version)

    result = script.pip(
        "install", "--ignore-installed", "--no-index", "--find-links",
        script.scratch_path, "dep==1", "dep[feature]",
    )

    script.assert_installed(dep="1.0")
    assert "Nab catalogue success" in result.stdout
    assert "dep-16.0-py3-none-any.whl" not in result.stdout


def test_extras_coupling_reopens_versions_after_a_base_backtrack(
    script: PipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(script, "chooser", "2.0", depends=["dep==1"])
    create_basic_wheel_for_package(script, "chooser", "1.0", depends=["dep==2"])
    create_basic_wheel_for_package(script, "leaf", "1.0")
    for version in range(1, 7):
        feature_wheel(script, version, "leaf==1" if version == 2 else "missing")

    result = script.pip(
        "install", "--ignore-installed", "--no-index", "--find-links",
        script.scratch_path, "chooser", "dep[feature]",
    )

    script.assert_installed(chooser="1.0", dep="2.0", leaf="1.0")
    assert "chooser-2.0-py3-none-any.whl" in result.stdout
    assert "Nab catalogue success" in result.stdout
