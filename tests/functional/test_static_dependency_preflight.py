import json
from pathlib import Path

import pytest

from pip._internal.utils.compat import tomllib

from tests.lib import PipTestEnvironment, create_basic_sdist_for_package
from tests.lib.wheel import make_wheel


def create_prefix_sdist(
    script: PipTestEnvironment,
    version: str,
    dependency: str,
    *,
    metadata_version: str = "2.6",
    dynamic: bool = True,
    dynamic_python: bool = False,
) -> Path:
    return create_basic_sdist_for_package(
        script,
        "prefix_demo",
        version,
        extra_files={
            "PKG-INFO": (
                f"Metadata-Version: {metadata_version}\nName: prefix-demo\n"
                f"Version: {version}\nRequires-Python: >=3.10\n"
                f"Requires-Dist: {dependency}\nProvides-Extra: speed\n"
                + ("Dynamic: Requires-Dist\n" if dynamic else "")
                + ("Dynamic: Requires-Python\n" if dynamic_python else "")
                + "\n"
            ),
            "pyproject.toml": (
                '[build-system]\nrequires = []\nbuild-backend = "backend"\n'
                'backend-path = ["."]\n'
            ),
            "backend.py": (
                'raise RuntimeError("unneeded dependency backend executed")\n'
            ),
        },
    )


@pytest.mark.parametrize("command", ["download", "lock", "install", "report"])
@pytest.mark.parametrize(
    "domain",
    [
        "root",
        "constraint",
        "eager-root",
        "extra-root",
        "invalid-prefix",
        "static-dependency",
        "range-root",
    ],
)
def test_static_dependency_prefix_rejects_without_backend(
    script: PipTestEnvironment, command: str, domain: str
) -> None:
    create_prefix_sdist(script, "1.0", "dependency==1.0", dynamic=False)
    dependency = "dependency==2.0"
    if domain == "extra-root":
        dependency += '; extra == "speed"'
    elif domain == "invalid-prefix":
        dependency = "dependency>=broken"
    newer = create_prefix_sdist(
        script,
        "2.0",
        dependency,
        metadata_version="2.2" if domain == "static-dependency" else "2.6",
        dynamic=domain != "static-dependency",
        dynamic_python=domain == "static-dependency",
    )
    for version in ("1.0", "2.0"):
        make_wheel("dependency", version).save_to_dir(script.scratch_path)
    args: list[str | Path] = ["install" if command == "report" else command]
    args.extend(["--no-index", "--find-links", script.scratch_path])
    if command in {"install", "report"}:
        args.extend(["--dry-run", "--ignore-installed"])
    if command == "report":
        args.extend(["--report", "report.json"])
    if command == "download":
        args.extend(["--dest", "downloaded"])
    source = "prefix-demo[speed]" if domain == "extra-root" else "prefix-demo"
    args.append(newer if domain == "eager-root" else source)
    if domain == "constraint":
        args.extend(
            [
                "--constraint",
                script.temporary_file("constraints.txt", "dependency==1.0"),
            ]
        )
    else:
        args.append("dependency<2" if domain == "range-root" else "dependency==1.0")

    result = script.pip(
        *args,
        expect_error=domain == "eager-root",
        allow_stderr_warning=command == "lock"
        or domain in {"eager-root", "invalid-prefix"},
    )

    if domain == "eager-root":
        assert "unneeded dependency backend executed" not in result.stderr
        assert "ResolutionImpossible" in result.stderr
    elif command == "download":
        result.did_create(Path("scratch/downloaded/prefix_demo-1.0.tar.gz"))
        result.did_not_create(Path("scratch/downloaded/prefix_demo-2.0.tar.gz"))
    elif command == "lock":
        lock = tomllib.loads(script.scratch_path.joinpath("pylock.toml").read_text())
        assert {
            package["name"]: package.get("version") for package in lock["packages"]
        } == {"prefix-demo": "1.0", "dependency": "1.0"}
    else:
        assert "Would install dependency-1.0 prefix-demo-1.0" in result.stdout
        if command == "report":
            report = json.loads(script.scratch_path.joinpath("report.json").read_text())
            assert {
                package["metadata"]["name"]: package["metadata"]["version"]
                for package in report["install"]
            } == {"prefix-demo": "1.0", "dependency": "1.0"}


@pytest.mark.parametrize(
    "metadata_version, dependency, root",
    [
        ("2.6", "dependency==1.0+local", "dependency==1.0"),
        ("2.6", "dependency==1.0", "dependency==1.0+local"),
        ("2.6", "dependency==2.0", "dependency==2.0"),
        ("2.5", "dependency==2.0", "dependency==1.0"),
        ("2.6", "dependency==2.0", "dependency==1.0; python_version < '0'"),
        ("2.6", "dependency==2.0; python_version < '0'", "dependency==1.0"),
        ("2.6", "dependency==2.0; extra == 'speed'", "dependency==1.0"),
    ],
)
def test_unproven_dependency_conflicts_still_prepare_backend(
    script: PipTestEnvironment,
    metadata_version: str,
    dependency: str,
    root: str,
) -> None:
    create_prefix_sdist(script, "2.0", dependency, metadata_version=metadata_version)
    for version in ("1.0", "1.0+local", "2.0"):
        make_wheel("dependency", version).save_to_dir(script.scratch_path)
    result = script.pip(
        "download",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "prefix-demo",
        root,
        expect_error=True,
    )

    assert "unneeded dependency backend executed" in result.stderr


def test_no_deps_report_still_requires_complete_metadata(
    script: PipTestEnvironment,
) -> None:
    create_prefix_sdist(script, "2.0", "dependency==2.0")
    make_wheel("dependency", "1.0").save_to_dir(script.scratch_path)

    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--dry-run",
        "--ignore-installed",
        "--no-deps",
        "--report",
        "report.json",
        "prefix-demo",
        "dependency==1.0",
        expect_error=True,
    )

    assert "unneeded dependency backend executed" in result.stderr
    result.did_not_create(Path("scratch/report.json"))


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("wheel_first", [False, True])
def test_url_root_uses_actual_wheel_metadata_version(
    script: PipTestEnvironment, named: bool, wheel_first: bool
) -> None:
    create_prefix_sdist(script, "2.0", "dependency==2.0")
    wheel = Path(
        make_wheel(
            "dependency", "1.0", metadata_updates={"Version": "2.0"}
        ).save_to_dir(script.scratch_path)
    )
    wheel_requirement = f"dependency @ {wheel.as_uri()}" if named else wheel.as_uri()
    roots = (
        [wheel_requirement, "prefix-demo"]
        if wheel_first
        else ["prefix-demo", wheel_requirement]
    )

    result = script.pip(
        "download",
        "--no-index",
        "--find-links",
        script.scratch_path,
        *roots,
        expect_error=True,
    )

    assert "unneeded dependency backend executed" in result.stderr
