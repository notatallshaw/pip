import pytest

from tests.lib import PipTestEnvironment, create_basic_wheel_for_package
from tests.lib.wheel import make_wheel


@pytest.mark.parametrize("requirement", ["app", "app[feature]"])
def test_catalogue_resolves_native_extras_and_python_requirements(
    script: PipTestEnvironment, requirement: str
) -> None:
    create_basic_wheel_for_package(script, "dep", "1.0")
    create_basic_wheel_for_package(script, "dep", "2.0")
    app = make_wheel(
        name="app",
        version="1.0",
        metadata=(
            "Metadata-Version: 2.1\nName: app\nVersion: 1.0\n"
            "Requires-Python: >=3.10\nProvides-Extra: feature\n"
            "Requires-Dist: dep>=1\n"
            'Requires-Dist: dep<2; extra == "feature"\n'
        ),
    )
    app.save_to(script.scratch_path / "app-1.0-py3-none-any.whl")
    result = script.pip(
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        script.scratch_path,
        requirement,
    )
    assert "Nab catalogue success" in result.stdout
    script.assert_installed(app="1.0", dep="1.0" if "[" in requirement else "2.0")


def test_catalogue_preserves_base_constraints_with_extras(
    script: PipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(script, "dep", "1.0")
    create_basic_wheel_for_package(script, "dep", "2.0")
    script.scratch_path.joinpath("constraints.txt").write_text("dep<2\n")
    result = script.pip(
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--constraint",
        script.scratch_path / "constraints.txt",
        "dep[unknown]",
        allow_stderr_warning=True,
    )
    assert "Nab catalogue success" in result.stdout
    script.assert_installed(dep="1.0")


def test_catalogue_retries_an_exact_prerelease(script: PipTestEnvironment) -> None:
    create_basic_wheel_for_package(script, "dep", "1.0a1")
    result = script.pip(
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "dep==1.0a1",
    )
    assert "Nab catalogue fallback: ResolutionError" in result.stdout
    script.assert_installed(dep="1.0a1")
