import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from tests.lib import PipTestEnvironment, TestData, create_basic_wheel_for_package
from tests.lib.resolver import tracked_install
from tests.lib.wheel import make_wheel


def test_invalid_installed_metadata_stops_resolution(
    script: PipTestEnvironment, data: TestData
) -> None:
    with zipfile.ZipFile(
        data.packages / "require_invalid_version-1.0-py3-none-any.whl"
    ) as wheel:
        wheel.extractall(script.site_packages_path)

    result = script.pip(
        "install", "--no-index", "require-invalid-version", expect_error=True
    )

    assert "invalid-installed-package" in result.stderr


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


@pytest.mark.parametrize("version, on_index", [("2rc1", False), ("1+local", True)])
def test_catalogue_keeps_installed_versions(
    script: PipTestEnvironment, version: str, on_index: bool
) -> None:
    install_initial(script, version, ["child==1"])
    create_basic_wheel_for_package(script, "child", "1")
    create_basic_wheel_for_package(script, "sample", "3")
    if on_index:
        create_basic_wheel_for_package(script, "sample", version)

    script.pip_install_local("sample", find_links=script.scratch_path)

    script.assert_installed(sample=version, child="1")


def test_ignore_installed_keeps_both_distribution_records(
    script: PipTestEnvironment,
) -> None:
    install_initial(script, "1", [])
    create_basic_wheel_for_package(script, "sample", "2")

    script.pip_install_local(
        "--ignore-installed", "sample", find_links=script.scratch_path
    )

    assert (script.site_packages_path / "sample-1.dist-info").is_dir()
    assert (script.site_packages_path / "sample-2.dist-info").is_dir()


def test_catalogue_can_replace_an_installed_dependency_conflict(
    script: PipTestEnvironment,
) -> None:
    install_initial(script, "1", ["child==9"])
    create_basic_wheel_for_package(script, "sample", "2", depends=["child==1"])
    create_basic_wheel_for_package(script, "child", "1")

    script.pip_install_local("sample", "child==1", find_links=script.scratch_path)

    script.assert_installed(sample="2", child="1")


def test_catalogue_widening_preserves_installed_only_version(
    script: PipTestEnvironment,
) -> None:
    install_initial(script, "2", ["child==1"])
    create_basic_wheel_for_package(script, "sample", "1", depends=["child==9"])
    create_basic_wheel_for_package(script, "sample", "3", depends=["child==9"])
    create_basic_wheel_for_package(script, "child", "1")

    result = script.pip_install_local(
        "--upgrade", "sample", "child==1", find_links=script.scratch_path
    )

    assert "Nab source-domain restart" not in result.stdout
    script.assert_installed(sample="2", child="1")


def test_catalogue_constraints_can_replace_an_installation(
    script: PipTestEnvironment, tmp_path: Path
) -> None:
    install_initial(script, "1", [])
    create_basic_wheel_for_package(script, "sample", "2")
    constraint = tmp_path / "constraint.txt"
    constraint.write_text("sample>=2\n")

    script.pip_install_local(
        "--constraint", constraint, "sample", find_links=script.scratch_path
    )

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
    script.run("python", str(probe), "install", "--no-index", *options, requirement)

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

    script.pip_install_local(
        *(("--upgrade",) if upgrade else ()),
        *requirements,
        find_links=script.scratch_path,
    )

    expected = "2" if upgrade else "1"
    script.assert_installed(sample=expected, child=expected)


def test_catalogue_can_ignore_installed_dependency_metadata(
    script: PipTestEnvironment,
) -> None:
    install_initial(script, "1", ["missing==1"])

    script.pip("install", "--no-index", "--no-deps", "sample")

    script.assert_installed(sample="1")
    script.assert_not_installed("missing")


def test_deferred_yanked_pin_is_checked_before_replacing_an_installation(
    script: PipTestEnvironment,
) -> None:
    install_initial(script, "1", [])
    wheel = create_basic_wheel_for_package(script, "sample", "2")
    listing = script.scratch_path / "links.html"
    listing.write_text(f'<a href="{wheel.name}" data-yanked="withdrawn">wheel</a>')

    script.pip_install_local("sample==2", find_links=listing, allow_stderr_warning=True)

    script.assert_installed(sample="2")


def test_installed_self_replacement_uses_selected_metadata(
    script: PipTestEnvironment,
) -> None:
    install_initial(script, "1", ["sample>=2", "olddep==1"])
    create_basic_wheel_for_package(script, "sample", "2")
    create_basic_wheel_for_package(script, "other", "2", depends=["olddep>=2"])
    create_basic_wheel_for_package(script, "other", "1")
    for version in ("1", "2"):
        create_basic_wheel_for_package(script, "olddep", version)

    script.pip_install_local("sample", "other", find_links=script.scratch_path)

    script.assert_installed(sample="2", other="2", olddep="2")
    script.pip("check")


def test_satisfied_installed_self_requirement_stays_in_the_catalogue(
    script: PipTestEnvironment,
) -> None:
    install_initial(script, "1", ["sample>=1"])

    script.pip("install", "--no-index", "sample")

    script.assert_installed(sample="1")


@pytest.mark.parametrize("version", ["1", "1.0"])
def test_index_metadata_replacement_updates_the_installed_package(
    script: PipTestEnvironment,
    version: str,
) -> None:
    initial = Path(
        make_wheel(
            "dep",
            "1",
            metadata_updates={"Requires-Dist": ["missing==1"]},
            extra_files={"dep/__init__.py": "marker = 'installed'\n"},
        ).save_to_dir(script.scratch_path)
    )
    script.pip("install", "--no-index", "--no-deps", initial)
    index = script.scratch_path / "index"
    index.mkdir()
    replacement = Path(
        make_wheel(
            "dep",
            version,
            extra_files={"dep/__init__.py": "marker = 'index'\n"},
        ).save_to_dir(index)
    )
    requirements: list[str | Path] = ["dep"]
    expected = {"dep": replacement}
    if version == "1.0":
        parent = create_basic_wheel_for_package(
            script, "other", "2", depends=["dep===1.0"]
        )
        requirements.append(parent)
        expected["other"] = parent
    report = script.scratch_path / "report.json"

    result = tracked_install(
        script,
        *requirements,
        find_links=index,
        ignore_installed=False,
        options=("--report", str(report)),
    )

    selected = {
        item["metadata"]["name"]: item
        for item in json.loads(report.read_text())["install"]
    }
    assert set(selected) == set(expected)
    for name, artifact in expected.items():
        download = selected[name]["download_info"]
        assert download["url"] == artifact.as_uri()
        assert (
            download["archive_info"]["hashes"]["sha256"]
            == hashlib.sha256(artifact.read_bytes()).hexdigest()
        )
    assert not any(
        " @ " in line
        for line in result.stdout.splitlines()
        if line.startswith("DEPENDENCY_READ ")
    )
    script.run(
        "python",
        "-c",
        (
            "import dep; from importlib.metadata import distribution; "
            "assert dep.marker == 'index'; assert not distribution('dep').requires"
        ),
    )
    script.assert_installed(dep=version)
    script.pip("check")


def test_direct_url_discovered_after_installed_candidate(
    script: PipTestEnvironment,
) -> None:
    """Read new dependencies from a direct artifact at an installed version."""
    wheel = create_basic_wheel_for_package(script, "dep", "1.0")
    script.pip("install", "--no-index", wheel)

    source = script.scratch_path / "direct"
    source.mkdir()
    replacement = create_basic_wheel_for_package(
        script, "dep", "1.0", depends=["child==1.0"]
    )
    replacement = replacement.rename(source / replacement.name)
    create_basic_wheel_for_package(script, "child", "1.0")
    create_basic_wheel_for_package(
        script, "app", "1.0", depends=[f"dep @ {replacement.as_uri()}"]
    )

    script.pip_install_local("dep", "app", find_links=script.scratch_path)
    script.assert_installed(app="1.0", dep="1.0", child="1.0")
