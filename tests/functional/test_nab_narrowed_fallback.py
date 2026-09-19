import hashlib

import pytest

from tests.lib import (
    PipTestEnvironment,
    create_basic_sdist_for_package,
    create_basic_wheel_for_package,
)
from tests.lib.wheel import make_wheel


def test_known_dependency_url_uses_fixed_input_source(
    script: PipTestEnvironment,
) -> None:
    dep = create_basic_wheel_for_package(script, "dep", "1")
    make_wheel(
        name="app",
        version="1",
        metadata=(
            "Metadata-Version: 2.2\nName: app\nVersion: 1\n"
            f"Requires-Dist: dep @ {dep.as_uri()}\n"
        ),
    ).save_to_dir(script.scratch_path)

    result = script.pip(
        "install", "--no-index", "--find-links", script.scratch_path, "app", dep
    )

    assert "Nab catalogue success" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(app="1", dep="1")


@pytest.mark.parametrize("active", [False, True])
def test_source_constraint_is_prepared_only_when_required(
    script: PipTestEnvironment, active: bool
) -> None:
    app = create_basic_wheel_for_package(script, "app", "1")
    create_basic_wheel_for_package(script, "app", "2")
    create_basic_wheel_for_package(script, "other", "1")
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"app @ {app.as_uri()}\n")
    if not active:
        app.unlink()

    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "-c",
        constraints,
        "app" if active else "other",
    )

    assert "Nab catalogue success" in result.stdout
    script.assert_installed(**({"app": "1"} if active else {"other": "1"}))


@pytest.mark.parametrize("valid", [False, True])
def test_fixed_hash_policy_is_verified_on_fast_path(
    script: PipTestEnvironment, valid: bool
) -> None:
    wheel = create_basic_wheel_for_package(script, "app", "1")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest() if valid else "0" * 64
    requirements = script.scratch_path / "requirements.txt"
    requirements.write_text(f"app==1 --hash=sha256:{digest}\n")

    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "-r",
        requirements,
        expect_error=not valid,
    )

    assert "Nab catalogue fallback" not in result.stdout
    if valid:
        assert "Nab catalogue success" in result.stdout
        script.assert_installed(app="1")
    else:
        assert "DO NOT MATCH THE HASHES" in result.stderr
        script.assert_not_installed("app")


def test_conflicting_input_sources_fail_without_native_solve(
    script: PipTestEnvironment,
) -> None:
    first = create_basic_wheel_for_package(script, "app", "1")
    alternate = script.scratch_path / "alternate"
    alternate.mkdir()
    second = alternate / first.name
    second.write_bytes(first.read_bytes())

    result = script.pip("install", "--no-index", first, second, expect_error=True)

    assert "ResolutionImpossible" in result.stderr
    assert "Nab catalogue fallback" not in result.stdout


def test_contradictory_root_ranges_fail_without_native_solve(
    script: PipTestEnvironment,
) -> None:
    result = script.pip("install", "--no-index", "app<1", "app>=2", expect_error=True)

    assert "ResolutionImpossible" in result.stderr
    assert "Nab catalogue fallback" not in result.stdout


def test_complete_dependency_domain_certifies_failure(
    script: PipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(script, "app", "1", depends=["missing==1"])

    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "app",
        expect_error=True,
    )

    assert "Nab catalogue certified failure" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    assert "No matching distribution found for missing==1" in result.stderr


def test_fixed_url_conflict_reports_both_requirements(
    script: PipTestEnvironment,
) -> None:
    direct = create_basic_wheel_for_package(script, "dep", "1")
    alternate = script.scratch_path / "alternate"
    alternate.mkdir()
    other = alternate / direct.name
    other.write_bytes(direct.read_bytes())
    parent = script.scratch_path / "app-1-py3-none-any.whl"
    make_wheel(
        name="app",
        version="1",
        metadata=(
            "Metadata-Version: 2.2\nName: app\nVersion: 1\n"
            f"Requires-Dist: dep @ {other.as_uri()}\n"
        ),
    ).save_to(parent)

    result = script.pip("install", "--no-index", parent, direct, expect_error=True)

    assert "ResolutionImpossible" in result.stderr
    assert "app 1 depends on dep" in result.stdout
    assert str(direct) in result.stdout
    assert str(other) in result.stdout
    assert "Nab catalogue fallback" not in result.stdout


@pytest.mark.parametrize(
    "invalid_field,expected", [("Version", "2.0"), ("Requires-Dist", "1.0")]
)
def test_metadata_rejection_scope_preserves_artifact_alternatives(
    script: PipTestEnvironment, invalid_field: str, expected: str
) -> None:
    create_basic_wheel_for_package(script, "pkg", "1.0")
    create_basic_sdist_for_package(script, "pkg", "2.0")
    rejected = script.scratch_path / "pkg-2.0-py2.py3-none-any.whl"
    make_wheel(
        name="pkg",
        version="2.0",
        metadata_updates={
            invalid_field: "9.0" if invalid_field == "Version" else "broken>=?"
        },
    ).save_to(rejected)

    result = script.pip(
        "install",
        "--no-index",
        "--no-build-isolation",
        "--find-links",
        script.scratch_path,
        "pkg",
        allow_stderr_warning=True,
    )

    assert "Nab catalogue success" in result.stdout
    script.assert_installed(pkg=expected)
