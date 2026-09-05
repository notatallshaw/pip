from pathlib import Path

import pytest

from tests.lib import PipTestEnvironment, create_basic_wheel_for_package
from tests.lib.wheel import make_wheel


def _self_wheel(path: Path, version: str, dependencies: list[str]) -> None:
    """Write self-reference metadata without folding its long file URI."""
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = f"Metadata-Version: 2.1\nName: selfpkg\nVersion: {version}\n"
    metadata += "".join(f"Requires-Dist: {dependency}\n" for dependency in dependencies)
    make_wheel(name="selfpkg", version=version, metadata=metadata).save_to(path)


def test_named_install_of_distribution_requiring_its_own_url(
    script: PipTestEnvironment,
) -> None:
    wheel = script.scratch_path / "selfpkg-1-py3-none-any.whl"
    _self_wheel(wheel, "1", [f"selfpkg @ {wheel.as_uri()}"])
    script.pip("install", "--no-index", wheel)
    script.pip("install", "--no-index", "selfpkg")
    script.assert_installed(selfpkg="1")


@pytest.mark.parametrize("version", ["1", "2"])
@pytest.mark.parametrize("keep_self", [False, True])
def test_self_url_replacement_keeps_its_own_metadata(
    script: PipTestEnvironment, version: str, keep_self: bool
) -> None:
    initial = script.scratch_path / "initial" / "selfpkg-1-py3-none-any.whl"
    target = script.scratch_path / "target" / f"selfpkg-{version}-py3-none-any.whl"
    requirement = f"selfpkg @ {target.as_uri()}"
    _self_wheel(initial, "1", [requirement])
    _self_wheel(target, version, [*([requirement] if keep_self else []), "child==1"])
    create_basic_wheel_for_package(script, "child", "1")
    script.pip("install", "--no-index", "--no-deps", initial)
    script.pip("install", "--no-index", "--find-links", script.scratch_path, "selfpkg")
    script.assert_installed(selfpkg=version, child="1")


@pytest.mark.parametrize("fallback", [False, True])
def test_self_url_from_an_unusable_installed_candidate_is_not_retained(
    script: PipTestEnvironment, fallback: bool
) -> None:
    initial = script.scratch_path / "initial" / "selfpkg-1-py3-none-any.whl"
    target = script.scratch_path / "target" / "selfpkg-2-py3-none-any.whl"
    requirement = f"selfpkg @ {target.as_uri()}"
    _self_wheel(initial, "1", [requirement, "missing==1"])
    _self_wheel(target, "2", [requirement])
    script.pip("install", "--no-index", "--no-deps", initial)
    if fallback:
        create_basic_wheel_for_package(script, "selfpkg", "3")
    script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "selfpkg",
        expect_error=not fallback,
    )
    script.assert_installed(selfpkg="3" if fallback else "1")


def test_impossible_installed_dependencies_do_not_prepare_a_later_url(
    script: PipTestEnvironment,
) -> None:
    initial = script.scratch_path / "initial" / "selfpkg-1-py3-none-any.whl"
    target = script.scratch_path / "target" / "selfpkg-2-py3-none-any.whl"
    missing = script.scratch_path / "missing-1-py3-none-any.whl"
    requirement = f"selfpkg @ {target.as_uri()}"
    _self_wheel(
        initial,
        "1",
        [requirement, "olddep<1", "olddep>=2", f"missing @ {missing.as_uri()}"],
    )
    _self_wheel(target, "2", [requirement])
    create_basic_wheel_for_package(script, "selfpkg", "3")
    script.pip("install", "--no-index", "--no-deps", initial)
    script.pip("install", "--no-index", "--find-links", script.scratch_path, "selfpkg")
    script.assert_installed(selfpkg="3")


@pytest.mark.parametrize("broken", [False, True])
def test_replaced_dependencies_are_checked_but_not_installed(
    script: PipTestEnvironment, broken: bool
) -> None:
    initial = script.scratch_path / "initial" / "selfpkg-1-py3-none-any.whl"
    target = script.scratch_path / "target" / "selfpkg-2-py3-none-any.whl"
    requirement = f"selfpkg @ {target.as_uri()}"
    _self_wheel(initial, "1", [requirement, "olddep==1"])
    _self_wheel(target, "2", [requirement])
    create_basic_wheel_for_package(
        script, "olddep", "1", depends=["missing==1"] if broken else []
    )
    script.pip("install", "--no-index", "--no-deps", initial)
    script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "selfpkg",
        expect_error=broken,
    )
    script.assert_installed(selfpkg="1" if broken else "2")
    script.assert_not_installed("olddep")


@pytest.mark.parametrize("same_url", [False, True])
def test_later_roots_preserve_self_replacement_obligations(
    script: PipTestEnvironment, same_url: bool
) -> None:
    initial = script.scratch_path / "initial" / "selfpkg-1-py3-none-any.whl"
    target = script.scratch_path / "target" / "selfpkg-2-py3-none-any.whl"
    requirement = f"selfpkg @ {target.as_uri()}"
    _self_wheel(initial, "1", [requirement, "olddep==1"])
    _self_wheel(target, "2", [requirement])
    create_basic_wheel_for_package(script, "olddep", "1")
    create_basic_wheel_for_package(script, "olddep", "2")
    for version, dependencies in [("2", ["olddep>=2"]), ("1", [])]:
        metadata = f"Metadata-Version: 2.1\nName: other\nVersion: {version}\n"
        metadata += "".join(
            f"Requires-Dist: {dependency}\n"
            for dependency in [*dependencies, *([requirement] if same_url else [])]
        )
        make_wheel(name="other", version=version, metadata=metadata).save_to(
            script.scratch_path / f"other-{version}-py3-none-any.whl"
        )
    script.pip("install", "--no-index", "--no-deps", initial)
    script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "selfpkg",
        "other",
    )
    script.assert_installed(selfpkg="2", other="1")
    script.assert_not_installed("olddep")
