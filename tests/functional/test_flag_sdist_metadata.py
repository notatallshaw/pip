from pathlib import Path

import pytest

from tests.lib import PipTestEnvironment, create_basic_sdist_for_package
from tests.lib.wheel import make_wheel


@pytest.mark.parametrize(
    "dynamic_fields, flags",
    [
        (["Requires-Dist", "Provides-Extra"], ["--no-deps"]),
        (["Requires-Python"], ["--ignore-requires-python"]),
        (
            ["Requires-Dist", "Provides-Extra", "Requires-Python"],
            ["--no-deps", "--ignore-requires-python"],
        ),
    ],
)
def test_download_does_not_prepare_unused_dynamic_metadata(
    script: PipTestEnvironment, dynamic_fields: list[str], flags: list[str]
) -> None:
    metadata = "Metadata-Version: 2.2\nName: flag-demo\nVersion: 1.0\n"
    for field in dynamic_fields:
        metadata += f"Dynamic: {field}\n"
    metadata += (
        "Requires-Python: "
        + ("not a specifier" if "Requires-Python" in dynamic_fields else ">=3.9")
        + "\nRequires-Dist: "
        + (
            "dependency>=broken"
            if "Requires-Dist" in dynamic_fields
            else "dependency==1.0"
        )
        + "\nProvides-Extra: speed\n"
    )
    archive = create_basic_sdist_for_package(
        script,
        "flag_demo",
        "1.0",
        extra_files={
            "PKG-INFO": metadata,
            "pyproject.toml": (
                '[build-system]\nrequires = []\nbuild-backend = "unavailable_backend"\n'
            ),
        },
    )
    dependency_wheel = Path(
        make_wheel("dependency", "1.0").save_to_dir(script.scratch_path)
    )

    result = script.pip(
        "download",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--dest",
        "downloaded",
        *flags,
        archive,
    )

    result.did_create("scratch/downloaded/flag_demo-1.0.tar.gz")
    dependency = script.scratch_path / "downloaded" / dependency_wheel.name
    assert dependency.exists() == ("--no-deps" not in flags)


@pytest.mark.parametrize(
    "metadata_version, dynamic",
    [("2.2", ""), ("2.6", "Dynamic: Requires-Dist\n")],
)
def test_no_deps_still_validates_known_static_dependency_metadata(
    script: PipTestEnvironment, metadata_version: str, dynamic: str
) -> None:
    archive = create_basic_sdist_for_package(
        script,
        "flag_demo",
        "1.0",
        extra_files={
            "PKG-INFO": (
                f"Metadata-Version: {metadata_version}\nName: flag-demo\nVersion: 1.0\n"
                f"{dynamic}"
                "Requires-Dist: dependency>=broken\n"
            ),
            "pyproject.toml": (
                '[build-system]\nrequires = []\nbuild-backend = "unavailable_backend"\n'
            ),
        },
    )

    result = script.pip(
        "download", "--no-index", "--no-deps", archive, expect_error=True
    )

    assert "invalid metadata" in result.stderr
    assert "dependency>=broken" in result.stderr
    assert "backend-unavailable" not in result.stderr


def test_flags_do_not_reduce_full_report_metadata(script: PipTestEnvironment) -> None:
    archive = create_basic_sdist_for_package(
        script,
        "flag_demo",
        "1.0",
        extra_files={
            "PKG-INFO": (
                "Metadata-Version: 2.2\nName: flag-demo\nVersion: 1.0\n"
                "Dynamic: Summary\nSummary: provisional\n"
            ),
            "pyproject.toml": (
                '[build-system]\nrequires = []\nbuild-backend = "unavailable_backend"\n'
            ),
        },
    )

    result = script.pip(
        "install",
        "--no-index",
        "--dry-run",
        "--ignore-installed",
        "--no-deps",
        "--ignore-requires-python",
        "--report",
        "report.json",
        archive,
        expect_error=True,
    )

    assert "backend-unavailable" in result.stderr
