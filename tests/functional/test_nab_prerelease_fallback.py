"""Keep prerelease admission separate from learned final-version conflicts."""

import pytest

from tests.functional.test_nab_preparation_reuse import tracked_install
from tests.lib import PipTestEnvironment, create_basic_wheel_for_package


@pytest.mark.parametrize("working_final", [False, True])
@pytest.mark.parametrize("policy", ["default", "final", "pre"])
@pytest.mark.parametrize("grouped", [False, True])
def test_prerelease_after_matching_final_resolution_failure(
    script: PipTestEnvironment, policy: str, *, working_final: bool, grouped: bool
) -> None:
    create_basic_wheel_for_package(script, "dep", "1")
    create_basic_wheel_for_package(
        script, "dep", "2", depends=[] if working_final else ["missing==1"]
    )
    if grouped:
        create_basic_wheel_for_package(
            script, "dep", "2.0", depends=[] if working_final else ["missing==1"]
        )
    create_basic_wheel_for_package(script, "dep", "2rc1")
    options = {"default": (), "final": ("--only-final=dep",), "pre": ("--pre",)}
    failed = not working_final and policy != "pre"

    result = tracked_install(
        script, "dep>1", options=options[policy], expect_error=failed
    )

    assert "NATIVE True" not in result.stdout
    assert "NATIVE False" not in result.stdout
    if failed:
        script.assert_not_installed("dep")
    else:
        script.assert_installed(dep="2" if working_final else "2rc1")
        script.pip("check")


@pytest.mark.parametrize("grouped", [False, True])
def test_default_admission_keeps_the_final_with_an_older_parent(
    script: PipTestEnvironment, *, grouped: bool
) -> None:
    create_basic_wheel_for_package(script, "shared", "1")
    create_basic_wheel_for_package(script, "shared", "2")
    create_basic_wheel_for_package(script, "dep", "2", depends=["shared==2"])
    if grouped:
        create_basic_wheel_for_package(script, "dep", "2.0", depends=["shared==2"])
    create_basic_wheel_for_package(script, "dep", "2rc1", depends=["shared==1"])
    create_basic_wheel_for_package(script, "app", "1", depends=["shared==2"])
    create_basic_wheel_for_package(script, "app", "2", depends=["shared==1"])

    result = tracked_install(script, "app", "dep>1")

    assert "NATIVE True" not in result.stdout
    assert "NATIVE False" not in result.stdout
    assert "Nab catalogue success" in result.stdout
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


@pytest.mark.parametrize("declaration", ["dep!=2", "dep>=2rc1"])
@pytest.mark.parametrize("indirect", [False, True])
@pytest.mark.parametrize("grouped", [False, True])
def test_later_parent_can_admit_a_selected_prerelease(
    script: PipTestEnvironment, declaration: str, *, indirect: bool, grouped: bool
) -> None:
    create_basic_wheel_for_package(script, "dep", "1")
    create_basic_wheel_for_package(script, "dep", "2", depends=["missing==1"])
    if grouped:
        create_basic_wheel_for_package(script, "dep", "2.0", depends=["missing==1"])
    create_basic_wheel_for_package(script, "dep", "2rc1")
    create_basic_wheel_for_package(script, "app", "1")
    if indirect:
        create_basic_wheel_for_package(script, "bridge", "1", depends=[declaration])
    create_basic_wheel_for_package(
        script, "app", "2", depends=["bridge"] if indirect else [declaration]
    )

    result = tracked_install(script, "dep>1", "app")

    assert "NATIVE True" not in result.stdout
    assert "NATIVE False" not in result.stdout
    assert "Nab catalogue success" in result.stdout
    script.assert_installed(app="2", dep="2rc1")
    script.pip("check")


@pytest.mark.parametrize("declaration", ["dep!=2", "dep>=2rc1"])
@pytest.mark.parametrize("grouped", [False, True])
def test_backtracking_a_parent_can_admit_the_same_prerelease(
    script: PipTestEnvironment, declaration: str, *, grouped: bool
) -> None:
    create_basic_wheel_for_package(script, "dep", "1")
    create_basic_wheel_for_package(script, "dep", "2", depends=["missing==1"])
    if grouped:
        create_basic_wheel_for_package(script, "dep", "2.0", depends=["missing==1"])
    create_basic_wheel_for_package(script, "dep", "2rc1")
    create_basic_wheel_for_package(script, "app", "2", depends=["dep>1"])
    create_basic_wheel_for_package(script, "app", "1", depends=[declaration])

    result = tracked_install(script, "app", "dep>1")

    assert "NATIVE True" not in result.stdout
    assert "NATIVE False" not in result.stdout
    assert "Nab catalogue success" in result.stdout
    script.assert_installed(app="1", dep="2rc1")
    script.pip("check")
