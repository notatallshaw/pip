"""Keep complete-plan policy checks in the native solver's original search."""

import pytest

from tests.lib import PipTestEnvironment, create_basic_wheel_for_package
from tests.lib.wheel import make_wheel

ADMISSION_PROBE = """\
import functools
import runpy
import sys
from pip._vendor.nab_resolver.candidate_provider import CandidateProvider
from pip._vendor.nab_resolver.resolver import Resolver as CoreResolver
from pip._internal.exceptions import InstallationError
from pip._internal.resolution.nab import resolver as resolver_module

mode = sys.argv.pop(1)
original_validate = CandidateProvider.validate_solution
original_native = resolver_module.Resolver._resolve_native

def validate(provider, solution, constraints=None):
    assert constraints is not None and "dep" in constraints
    assert original_validate(provider, solution, constraints)

    version = str(solution.pins["app"].version)
    print("VALIDATE " + version, flush=True)
    if mode == "error":
        raise InstallationError("transient policy read failed")
    return mode == "older" and version == "1.0"

def resolve_native(resolver, collected):
    print("NATIVE", flush=True)
    return original_native(resolver, collected)

CandidateProvider.validate_solution = validate
resolver_module.Resolver._resolve_native = resolve_native
if mode == "limit":
    resolver_module.NabResolver = functools.partial(CoreResolver, max_iterations=4)
runpy.run_module("pip", run_name="__main__", alter_sys=True)
"""


def policy_wheels(script: PipTestEnvironment) -> None:
    """Force native source selection using a real hidden URL dependency."""
    dependency = create_basic_wheel_for_package(script, "dep", "1.0")
    hidden = script.scratch_path / "hidden"
    hidden.mkdir()
    dependency = dependency.rename(hidden / dependency.name)

    for version in ("2.0", "1.0"):
        make_wheel(
            name="app",
            version=version,
            metadata=(
                f"Metadata-Version: 2.1\nName: app\nVersion: {version}\n"
                f"Requires-Dist: dep @ {dependency.as_uri()}\n"
            ),
        ).save_to_dir(script.scratch_path)


@pytest.mark.parametrize("mode", ["older", "reject", "error", "limit"])
def test_native_final_policy_uses_one_search(
    script: PipTestEnvironment, mode: str
) -> None:
    policy_wheels(script)
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text("dep>=1\n")
    probe = script.scratch_path / "admission_probe.py"
    probe.write_text(ADMISSION_PROBE)

    result = script.run(
        "python",
        str(probe),
        mode,
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        str(script.scratch_path),
        "--constraint",
        str(constraints),
        "app",
        expect_error=mode != "older",
        allow_stderr_warning=True if script.pip_expect_warning else None,
    )
    assert result.stdout.splitlines().count("NATIVE") == 1
    assert "VALIDATE 2.0" in result.stdout

    if mode == "older":
        script.assert_installed(app="1.0", dep="1.0")
        assert "VALIDATE 1.0" in result.stdout
    elif mode == "reject":
        assert "host rules reject the selection" in result.stderr
        assert "No matching distribution found" not in result.stderr
    elif mode == "error":
        assert "transient policy read failed" in result.stderr
        assert "VALIDATE 1.0" not in result.stdout
    else:
        assert "exceeded 4 iterations" in result.stderr
        assert "VALIDATE 1.0" not in result.stdout


def test_later_range_keeps_the_selected_installed_self_replacement(
    script: PipTestEnvironment,
) -> None:
    replacement = create_basic_wheel_for_package(
        script, "selfpkg", "2.0", depends=["child==1.0"]
    )
    hidden = script.scratch_path / "hidden"
    hidden.mkdir()
    replacement = replacement.rename(hidden / replacement.name)
    initial = make_wheel(
        name="selfpkg",
        version="1.0",
        metadata=(
            "Metadata-Version: 2.1\nName: selfpkg\nVersion: 1.0\n"
            f"Requires-Dist: selfpkg @ {replacement.as_uri()}\n"
        ),
    ).save_to_dir(script.scratch_path)
    create_basic_wheel_for_package(script, "child", "1.0")
    create_basic_wheel_for_package(script, "other", "1.0", depends=["selfpkg>=2"])
    script.pip("install", "--no-index", "--no-deps", initial)

    script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "selfpkg",
        "other",
    )
    script.assert_installed(selfpkg="2.0", child="1.0", other="1.0")
    script.pip("check")
