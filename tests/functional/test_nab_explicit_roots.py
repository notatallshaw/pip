import json
from contextlib import AbstractContextManager, nullcontext

import pytest

from tests.lib import (
    PipTestEnvironment,
    create_basic_wheel_for_package,
    create_test_package_with_setup,
)
from tests.lib.server import file_response, make_mock_server, server_running


@pytest.mark.parametrize("version", ["1", "2rc1"])
@pytest.mark.parametrize("source", ["wheel", "http", "directory", "editable"])
def test_explicit_root_uses_catalogue(
    script: PipTestEnvironment, source: str, version: str
) -> None:
    create_basic_wheel_for_package(script, "child", "1")
    context: AbstractContextManager[None] = nullcontext()
    if source in ("wheel", "http"):
        wheel = create_basic_wheel_for_package(
            script, "app", version, depends=["child==1"]
        )
        sources = script.scratch_path / "sources"
        sources.mkdir()
        wheel = wheel.rename(sources / wheel.name)
        if source == "http":
            server = make_mock_server()
            server.mock.return_value = file_response(wheel)
            context = server_running(server)
            requirements = [f"http://{server.host}:{server.port}/{wheel.name}"]
        else:
            requirements = [str(wheel)]
    else:
        project = create_test_package_with_setup(
            script, name="app", version=version, install_requires=["child==1"]
        )
        requirements = (
            ["--editable", str(project)] if source == "editable" else [str(project)]
        )
    report = script.scratch_path / "report.json"

    with context:
        result = script.pip(
            "install",
            "--no-index",
            "--no-build-isolation",
            "--find-links",
            script.scratch_path,
            "--report",
            report,
            *requirements,
        )

    assert "Nab catalogue success" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(app=version, child="1")
    app = next(
        item
        for item in json.loads(report.read_text())["install"]
        if item["metadata"]["name"] == "app"
    )
    assert app["requested"]
    assert app["is_direct"]
    if source == "editable":
        assert app["download_info"]["dir_info"]["editable"]


def test_explicit_root_supplies_transitive_extras(script: PipTestEnvironment) -> None:
    wheel = create_basic_wheel_for_package(
        script, "app", "1", extras={"feature": ["child==1"]}
    )
    sources = script.scratch_path / "sources"
    sources.mkdir()
    wheel = wheel.rename(sources / wheel.name)
    create_basic_wheel_for_package(script, "app", "2", depends=["missing==1"])
    create_basic_wheel_for_package(script, "bridge", "1", depends=["app[feature]"])
    create_basic_wheel_for_package(script, "child", "1")

    result = script.pip(
        "install", "--no-index", "--find-links", script.scratch_path, wheel, "bridge"
    )

    assert "Nab catalogue success" in result.stdout
    script.assert_installed(app="1", bridge="1", child="1")


def test_explicit_root_takes_precedence_over_installed_metadata(
    script: PipTestEnvironment,
) -> None:
    initial = create_basic_wheel_for_package(script, "app", "1", depends=["missing==1"])
    script.pip("install", "--no-index", "--no-deps", initial)
    wheel = create_basic_wheel_for_package(script, "app", "1", depends=["child==1"])
    create_basic_wheel_for_package(script, "child", "1")

    result = script.pip(
        "install",
        "--no-index",
        "--force-reinstall",
        "--find-links",
        script.scratch_path,
        wheel,
    )

    assert "Nab catalogue success" in result.stdout
    script.assert_installed(app="1", child="1")
    script.assert_not_installed("missing")


@pytest.mark.parametrize("direct_first", [False, True])
@pytest.mark.parametrize("specifier", [">=1", ">=2"])
def test_explicit_root_obeys_named_requirement(
    script: PipTestEnvironment, direct_first: bool, specifier: str
) -> None:
    wheel = create_basic_wheel_for_package(
        script, "app", "1", extras={"feature": ["child==1"]}
    )
    create_basic_wheel_for_package(script, "app", "2")
    create_basic_wheel_for_package(script, "child", "1")
    requirements = [f"app[feature] @ {wheel.as_uri()}", f"app{specifier}"]
    if not direct_first:
        requirements.reverse()

    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        *requirements,
        expect_error=specifier == ">=2",
    )

    if specifier == ">=2":
        assert "ResolutionImpossible" in result.stderr
        script.assert_not_installed("app", "child")
    else:
        assert "Nab catalogue success" in result.stdout
        script.assert_installed(app="1", child="1")
