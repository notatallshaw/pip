"""Keep prerelease admission separate from learned final-version conflicts."""

import pytest

from tests.functional.test_nab_preparation_reuse import tracked_install
from tests.lib import PipTestEnvironment, create_basic_wheel_for_package


@pytest.mark.parametrize("working_final", [False, True])
@pytest.mark.parametrize("policy", ["default", "final", "pre"])
def test_prerelease_after_matching_final_resolution_failure(
    script: PipTestEnvironment, policy: str, *, working_final: bool
) -> None:
    create_basic_wheel_for_package(script, "dep", "1")
    create_basic_wheel_for_package(
        script, "dep", "2", depends=[] if working_final else ["missing==1"]
    )
    create_basic_wheel_for_package(script, "dep", "2rc1")
    options = {"default": (), "final": ("--only-final=dep",), "pre": ("--pre",)}
    failed = not working_final and policy != "pre"

    result = tracked_install(
        script, "dep>1", options=options[policy], expect_error=failed
    )

    if not working_final and policy == "default":
        assert "NATIVE True" in result.stdout
        assert "NATIVE False" in result.stdout
    else:
        assert "NATIVE True" not in result.stdout
        assert "NATIVE False" not in result.stdout
    if failed:
        script.assert_not_installed("dep")
    else:
        script.assert_installed(dep="2" if working_final else "2rc1")
        script.pip("check")


def test_default_admission_keeps_the_final_with_an_older_parent(
    script: PipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(script, "shared", "1")
    create_basic_wheel_for_package(script, "shared", "2")
    create_basic_wheel_for_package(script, "dep", "2", depends=["shared==2"])
    create_basic_wheel_for_package(script, "dep", "2rc1", depends=["shared==1"])
    create_basic_wheel_for_package(script, "app", "1", depends=["shared==2"])
    create_basic_wheel_for_package(script, "app", "2", depends=["shared==1"])

    result = tracked_install(script, "app", "dep>1")

    assert "NATIVE True" in result.stdout
    script.assert_installed(app="1", dep="2", shared="2")
    script.pip("check")


def test_rejected_parent_cannot_grant_prerelease_admission(
    script: PipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(script, "dep", "1")
    create_basic_wheel_for_package(script, "dep", "2", depends=["missing==1"])
    create_basic_wheel_for_package(script, "dep", "2rc1")
    create_basic_wheel_for_package(script, "app", "1", depends=["dep>1"])
    create_basic_wheel_for_package(
        script, "app", "2", depends=["dep>=2rc1", "missing==2"]
    )

    tracked_install(script, "app", expect_error=True)

    script.assert_not_installed("app", "dep")


def test_original_constraint_can_admit_a_prerelease(
    script: PipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(script, "dep", "1")
    create_basic_wheel_for_package(script, "dep", "2", depends=["missing==1"])
    create_basic_wheel_for_package(script, "dep", "2rc1")
    constraint = script.scratch_path / "constraint.txt"
    constraint.write_text("dep!=2\n")

    result = tracked_install(script, "dep>1", options=("-c", str(constraint)))

    assert "NATIVE True" not in result.stdout
    assert "NATIVE False" not in result.stdout
    script.assert_installed(dep="2rc1")
    script.pip("check")
