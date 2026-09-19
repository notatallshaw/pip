from pathlib import Path

import pytest

from tests.lib import PipTestEnvironment, create_basic_wheel_for_package


def install_initial(
    script: PipTestEnvironment, version: str, dependencies: list[str]
) -> None:
    """Install sample with metadata independent of the later finder listing."""
    wheel = create_basic_wheel_for_package(
        script, "sample", version, depends=dependencies
    )
    initial = script.scratch_path / "initial"
    initial.mkdir()
    wheel = wheel.rename(initial / wheel.name)
    script.pip("install", "--no-index", "--no-deps", wheel)


@pytest.mark.parametrize("version", ["1", "2rc1", "1+local"])
@pytest.mark.parametrize("on_index", [False, True])
def test_catalogue_keeps_installed_versions(
    script: PipTestEnvironment, version: str, on_index: bool
) -> None:
    install_initial(script, version, ["child==1"])
    create_basic_wheel_for_package(script, "child", "1")
    create_basic_wheel_for_package(script, "sample", "3")
    if on_index:
        create_basic_wheel_for_package(script, "sample", version)

    result = script.pip(
        "install", "--no-index", "--find-links", script.scratch_path, "sample"
    )

    assert "Nab catalogue success" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(sample=version, child="1")


@pytest.mark.parametrize(
    "options,expected",
    [
        ([], "1"),
        (["--upgrade"], "2"),
        (["--force-reinstall"], "2"),
        (["--ignore-installed"], "2"),
    ],
)
def test_catalogue_installed_selection_policy(
    script: PipTestEnvironment, options: list[str], expected: str
) -> None:
    install_initial(script, "1", [])
    create_basic_wheel_for_package(script, "sample", "1")
    create_basic_wheel_for_package(script, "sample", "2")

    result = script.pip(
        "install", "--no-index", "--find-links", script.scratch_path, *options, "sample"
    )

    assert "Nab catalogue success" in result.stdout
    script.assert_installed(sample=expected)


@pytest.mark.parametrize("strategy,expected", [("only-if-needed", "1"), ("eager", "2")])
def test_catalogue_upgrades_dependencies_by_policy(
    script: PipTestEnvironment, strategy: str, expected: str
) -> None:
    install_initial(script, "1", [])
    create_basic_wheel_for_package(script, "sample", "1")
    create_basic_wheel_for_package(script, "sample", "2")
    create_basic_wheel_for_package(script, "app", "1", depends=["sample>=1"])

    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--upgrade",
        "--upgrade-strategy",
        strategy,
        "app",
    )

    assert "Nab catalogue success" in result.stdout
    script.assert_installed(app="1", sample=expected)


def test_catalogue_can_replace_an_installed_dependency_conflict(
    script: PipTestEnvironment,
) -> None:
    install_initial(script, "1", ["child==9"])
    create_basic_wheel_for_package(script, "sample", "2", depends=["child==1"])
    create_basic_wheel_for_package(script, "child", "1")

    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "sample",
        "child==1",
    )

    assert "Nab catalogue success" in result.stdout
    script.assert_installed(sample="2", child="1")


def test_catalogue_widening_preserves_installed_only_version(
    script: PipTestEnvironment,
) -> None:
    install_initial(script, "2", ["child==1"])
    create_basic_wheel_for_package(script, "sample", "1", depends=["child==9"])
    create_basic_wheel_for_package(script, "sample", "3", depends=["child==9"])
    create_basic_wheel_for_package(script, "child", "1")

    result = script.pip(
        "install",
        "--upgrade",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "sample",
        "child==1",
    )

    assert "Nab catalogue success" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(sample="2", child="1")


def test_catalogue_constraints_can_replace_an_installation(
    script: PipTestEnvironment, tmp_path: Path
) -> None:
    install_initial(script, "1", [])
    create_basic_wheel_for_package(script, "sample", "2")
    constraint = tmp_path / "constraint.txt"
    constraint.write_text("sample>=2\n")

    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--constraint",
        constraint,
        "sample",
    )

    assert "Nab catalogue success" in result.stdout
    script.assert_installed(sample="2")


@pytest.mark.parametrize("requirement", ["sample", "sample==1"])
@pytest.mark.parametrize("unused_constraint", [False, True])
def test_satisfied_installation_does_not_load_the_index(
    script: PipTestEnvironment, requirement: str, unused_constraint: bool
) -> None:
    install_initial(script, "1", [])
    probe = script.scratch_path / "no_listing.py"
    probe.write_text(
        "import runpy\n"
        "from pip._internal.index.package_finder import PackageFinder\n"
        "def unexpected_listing(*args, **kwargs):\n"
        "    raise AssertionError('installed resolution loaded the finder')\n"
        "PackageFinder.find_all_candidates = unexpected_listing\n"
        "runpy.run_module('pip', run_name='__main__', alter_sys=True)\n"
    )

    options = []
    if unused_constraint:
        constraint = script.scratch_path / "constraints.txt"
        constraint.write_text("unused==1\n")
        options = ["--constraint", str(constraint)]
    result = script.run(
        "python", str(probe), "install", "--no-index", *options, requirement
    )

    assert "Nab catalogue success" in result.stdout
    script.assert_installed(sample="1")


@pytest.mark.parametrize("upgrade", [False, True])
@pytest.mark.parametrize("with_bridge", [False, True])
def test_catalogue_installed_extras_share_the_base_candidate(
    script: PipTestEnvironment, upgrade: bool, with_bridge: bool
) -> None:
    installed = create_basic_wheel_for_package(
        script, "sample", "1", extras={"extra": ["child==1"]}
    )
    script.pip("install", "--no-index", installed)
    create_basic_wheel_for_package(
        script, "sample", "2", extras={"extra": ["child==2"]}
    )
    for version in ("1", "2"):
        create_basic_wheel_for_package(script, "child", version)

    requirements = ["sample[extra]"]
    if with_bridge:
        create_basic_wheel_for_package(script, "bridge", "1", depends=["sample>=1"])
        requirements.append("bridge")

    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        *(("--upgrade",) if upgrade else ()),
        *requirements,
    )

    assert "Nab catalogue success" in result.stdout
    expected = "2" if upgrade else "1"
    script.assert_installed(sample=expected, child=expected)


def test_catalogue_can_ignore_installed_dependency_metadata(
    script: PipTestEnvironment,
) -> None:
    install_initial(script, "1", ["missing==1"])

    result = script.pip("install", "--no-index", "--no-deps", "sample")

    assert "Nab catalogue success" in result.stdout
    script.assert_installed(sample="1")
    script.assert_not_installed("missing")


def test_deferred_yanked_pin_is_checked_before_replacing_an_installation(
    script: PipTestEnvironment,
) -> None:
    install_initial(script, "1", [])
    wheel = create_basic_wheel_for_package(script, "sample", "2")
    listing = script.scratch_path / "links.html"
    listing.write_text(f'<a href="{wheel.name}" data-yanked="withdrawn">wheel</a>')

    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        listing,
        "sample==2",
        allow_stderr_warning=True,
    )

    assert "yanked requirement pin" in result.stdout
    script.assert_installed(sample="2")


def test_installed_self_replacement_preserves_native_obligations(
    script: PipTestEnvironment,
) -> None:
    install_initial(script, "1", ["sample>=2", "olddep==1"])
    create_basic_wheel_for_package(script, "sample", "2")
    create_basic_wheel_for_package(script, "other", "2", depends=["olddep>=2"])
    create_basic_wheel_for_package(script, "other", "1")
    for version in ("1", "2"):
        create_basic_wheel_for_package(script, "olddep", version)

    result = script.pip(
        "install", "--no-index", "--find-links", script.scratch_path, "sample", "other"
    )

    assert "installed self replacement" in result.stdout
    script.assert_installed(sample="2", other="1")
    script.assert_not_installed("olddep")


def test_satisfied_installed_self_requirement_stays_in_the_catalogue(
    script: PipTestEnvironment,
) -> None:
    install_initial(script, "1", ["sample>=1"])

    result = script.pip("install", "--no-index", "sample")

    assert "Nab catalogue success" in result.stdout
    script.assert_installed(sample="1")
