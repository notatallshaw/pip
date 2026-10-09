"""Resolve replacements using dependencies of the selected distributions."""

import pytest

from tests.functional.test_nab_preparation_reuse import tracked_install
from tests.lib import PipTestEnvironment, create_basic_wheel_for_package


@pytest.mark.parametrize("with_groups", [False, True])
@pytest.mark.parametrize("self_satisfied", [False, True])
@pytest.mark.parametrize("transitive", [False, True])
def test_installed_self_requirement_uses_only_selected_metadata(
    script: PipTestEnvironment,
    *,
    with_groups: bool,
    self_satisfied: bool,
    transitive: bool,
) -> None:
    initial = create_basic_wheel_for_package(
        script,
        "app",
        "1",
        depends=["app>=1" if self_satisfied else "app>=2", "olddep==1"],
    )
    olddep = create_basic_wheel_for_package(script, "olddep", "1")
    script.pip("install", "--no-index", "--no-deps", initial, olddep)
    create_basic_wheel_for_package(script, "app", "2")
    create_basic_wheel_for_package(script, "other", "1")
    create_basic_wheel_for_package(script, "other", "2", depends=["olddep>=2"])
    create_basic_wheel_for_package(script, "olddep", "2")
    if with_groups:
        create_basic_wheel_for_package(script, "olddep", "1.0")
    if transitive:
        create_basic_wheel_for_package(script, "parent", "1", depends=["app"])

    result = tracked_install(
        script, "parent" if transitive else "app", "other", ignore_installed=False
    )

    assert "NATIVE True" not in result.stdout
    assert "NATIVE False" not in result.stdout
    if self_satisfied:
        script.assert_installed(app="1", other="1", olddep="1")
    else:
        script.assert_installed(app="2", other="2", olddep="2")
        script.pip("check")
