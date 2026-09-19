from pathlib import Path

from tests.lib import (
    PipTestEnvironment,
    TestPipResult,
    create_basic_sdist_for_package,
    create_basic_wheel_for_package,
)
from tests.lib.wheel import make_wheel

PREPARATION_PROBE = """\
import runpy
import sys
from pip._internal.resolution.nab.candidates import LinkCandidate
from pip._internal.resolution.nab.resolver import Resolver

original = LinkCandidate._prepare
original_resolve = Resolver._resolve_attempt

def prepare(candidate):
    print("PREPARE " + candidate.source_link.filename, flush=True)
    return original(candidate)

LinkCandidate._prepare = prepare

def resolve_native(resolver, collected, *, provisional):
    print("NATIVE " + str(provisional), flush=True)
    if sys.exc_info()[0] is not None:
        print("NATIVE_ACTIVE_EXCEPTION", flush=True)
    return original_resolve(resolver, collected, provisional=provisional)

Resolver._resolve_attempt = resolve_native
runpy.run_module("pip", run_name="__main__", alter_sys=True)
"""


def tracked_install(
    script: PipTestEnvironment,
    *requirements: str | Path,
    options: tuple[str, ...] = (),
    expect_error: bool = False,
    ignore_installed: bool = True,
) -> TestPipResult:
    """Run real pip while counting candidate preparation at its native boundary."""
    probe = script.scratch_path / "preparation_probe.py"
    probe.write_text(PREPARATION_PROBE)
    return script.run(
        "python",
        str(probe),
        "install",
        *(("--ignore-installed",) if ignore_installed else ()),
        "--no-index",
        "--find-links",
        str(script.scratch_path),
        *options,
        *(str(requirement) for requirement in requirements),
        expect_error=expect_error,
        allow_stderr_warning=True if script.pip_expect_warning else None,
    )


def late_url_wheels(script: PipTestEnvironment) -> tuple[Path, Path]:
    """Make an older app release supply the dependency missing from the index."""
    create_basic_wheel_for_package(script, "dep", "1.0")
    hidden = script.scratch_path / "hidden"
    hidden.mkdir()
    direct = create_basic_wheel_for_package(script, "dep", "3.0")
    direct = direct.rename(hidden / direct.name)
    newer = create_basic_wheel_for_package(script, "app", "2.0", depends=["dep>=2"])
    older = make_wheel(
        name="app",
        version="1.0",
        metadata=(
            "Metadata-Version: 2.1\nName: app\nVersion: 1.0\n"
            f"Requires-Dist: dep @ {direct.as_uri()}\n"
        ),
    )
    return Path(older.save_to_dir(script.scratch_path)), newer


def preparation_count(output: str, wheel: Path) -> int:
    """Count calls to the real LinkCandidate preparation method for one wheel."""
    return output.splitlines().count("PREPARE " + wheel.name)


def test_fallback_reuses_prepared_wheels_and_accepts_url_dependencies(
    script: PipTestEnvironment,
) -> None:
    older, newer = late_url_wheels(script)
    result = tracked_install(script, "app")

    script.assert_installed(app="1.0", dep="3.0")
    assert "CatalogueUnsupported: URL dependency" in result.stdout
    assert preparation_count(result.stdout, newer) == 1
    assert preparation_count(result.stdout, older) == 1


def test_unsatisfiable_fallback_reuses_prepared_wheel(
    script: PipTestEnvironment,
) -> None:
    app = create_basic_wheel_for_package(script, "app", "1.0", depends=["dep>=2"])
    create_basic_wheel_for_package(script, "dep", "1.0")
    result = tracked_install(script, "app", expect_error=True)

    assert "Nab catalogue fallback: ResolutionError" in result.stdout
    assert "No matching distribution found for dep>=2" in result.stdout + result.stderr
    assert preparation_count(result.stdout, app) == 1
    assert "NATIVE True" not in result.stdout
    assert result.stdout.splitlines().count("NATIVE False") == 1
    assert "NATIVE_ACTIVE_EXCEPTION" not in result.stdout


def test_definitive_fallback_discovers_a_url_after_static_failure(
    script: PipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(script, "dep", "1.0")
    hidden = script.scratch_path / "hidden"
    hidden.mkdir()
    direct = create_basic_wheel_for_package(script, "dep", "3.0")
    direct = direct.rename(hidden / direct.name)
    make_wheel(
        name="bridge",
        version="1.0",
        metadata=(
            "Metadata-Version: 2.1\nName: bridge\nVersion: 1.0\n"
            f"Requires-Dist: dep @ {direct.as_uri()}\n"
        ),
    ).save_to_dir(script.scratch_path)

    result = tracked_install(script, "dep>=2", "bridge")

    script.assert_installed(dep="3.0", bridge="1.0")
    assert "Nab catalogue fallback: ResolutionError" in result.stdout
    assert "NATIVE True" not in result.stdout
    assert result.stdout.splitlines().count("NATIVE False") == 1
    assert "NATIVE_ACTIVE_EXCEPTION" not in result.stdout


def test_fallback_rechecks_failed_metadata_in_native_order(
    script: PipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(script, "pkg", "1.0")
    create_basic_sdist_for_package(script, "pkg", "2.0")
    rejected = script.scratch_path / "pkg-2.0-py2.py3-none-any.whl"
    make_wheel(name="pkg", version="2.0", metadata_updates={"Version": "9.0"}).save_to(
        rejected
    )
    result = tracked_install(
        script, "pkg", options=("--prefer-binary", "--no-build-isolation")
    )

    script.assert_installed(pkg="1.0")
    assert "CatalogueUnsupported: artifact preparation rejected" in result.stdout
    assert preparation_count(result.stdout, rejected) == 2


def test_explicit_root_restores_native_dependency_handling(
    script: PipTestEnvironment,
) -> None:
    older, _ = late_url_wheels(script)
    result = tracked_install(script, older)

    script.assert_installed(app="1.0", dep="3.0")
    assert "CatalogueUnsupported: explicit root" in result.stdout
    assert preparation_count(result.stdout, older) == 1
    assert result.stdout.splitlines().count("NATIVE True") == 1
    assert "NATIVE False" not in result.stdout


def test_empty_environment_uses_catalogue_without_ignore_installed(
    script: PipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(script, "pkg", "1.0")
    result = tracked_install(script, "pkg", ignore_installed=False)

    script.assert_installed(pkg="1.0")
    assert "Nab catalogue success" in result.stdout
    assert "NATIVE True" not in result.stdout
    assert "NATIVE False" not in result.stdout
