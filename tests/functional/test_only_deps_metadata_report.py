from __future__ import annotations

import base64
import json
import textwrap

import pytest

from tests.lib import PipTestEnvironment, create_basic_sdist_for_package, make_wheel


@pytest.mark.parametrize(
    "dry_run, ignore_python", [(False, False), (True, False), (True, True)]
)
def test_only_deps_report_prepares_metadata_only_for_reported_packages(
    script: PipTestEnvironment, dry_run: bool, ignore_python: bool
) -> None:
    metadata = (
        "Metadata-Version: 2.2\nName: dependency\nVersion: 1.0\n"
        "Summary: Generated dependency summary\n\n"
    )
    wheel = make_wheel(
        "dependency",
        "1.0",
        metadata=metadata,
        extra_files={"dependency/__init__.py": ""},
    )
    create_basic_sdist_for_package(
        script,
        "dependency",
        "1.0",
        extra_files={
            "PKG-INFO": (
                "Metadata-Version: 2.2\nName: dependency\nVersion: 1.0\n"
                "Dynamic: Summary\nSummary: Provisional dependency summary\n\n"
            ),
            "pyproject.toml": textwrap.dedent("""\
                [build-system]
                requires = []
                build-backend = "backend"
                backend-path = ["."]
                """),
            "backend-metadata": metadata,
            "wheel-data": base64.b64encode(wheel.as_bytes()).decode(),
            "backend.py": textwrap.dedent("""\
                import base64
                from pathlib import Path

                def prepare_metadata_for_build_wheel(
                    metadata_directory, config_settings=None
                ):
                    dist_info = Path(metadata_directory) / "dependency-1.0.dist-info"
                    dist_info.mkdir()
                    (dist_info / "METADATA").write_bytes(
                        Path("backend-metadata").read_bytes()
                    )
                    return dist_info.name

                def build_wheel(
                    wheel_directory, config_settings=None, metadata_directory=None
                ):
                    assert metadata_directory is not None
                    filename = "dependency-1.0-py2.py3-none-any.whl"
                    wheel_bytes = base64.b64decode(Path("wheel-data").read_bytes())
                    (Path(wheel_directory) / filename).write_bytes(wheel_bytes)
                    return filename
                """),
        },
    )
    root_python = (
        "Dynamic: Requires-Python\nRequires-Python: >=99\n"
        if ignore_python
        else "Requires-Python: >=3.10\n"
    )
    root = create_basic_sdist_for_package(
        script,
        "root",
        "1.0",
        extra_files={
            "PKG-INFO": (
                "Metadata-Version: 2.2\nName: root\nVersion: 1.0\n"
                "Requires-Dist: dependency==1.0\nDynamic: Summary\n"
                + root_python
                + "\n"
            ),
            "pyproject.toml": textwrap.dedent("""\
                [build-system]
                requires = []
                build-backend = "backend"
                backend-path = ["."]
                """),
            "backend.py": "raise RuntimeError('Excluded root backend ran')\n",
        },
    )
    dry_run_options = ["--dry-run", "--ignore-installed"] if dry_run else []
    python_options = ["--ignore-requires-python"] if ignore_python else []

    result = script.pip(
        "install",
        "--only-deps",
        "--report",
        "report.json",
        "--no-index",
        "--find-links",
        script.scratch_path,
        *dry_run_options,
        *python_options,
        root,
    )

    report = json.loads(script.scratch_path.joinpath("report.json").read_text())
    assert [item["metadata"]["name"] for item in report["install"]] == ["dependency"]
    assert report["install"][0]["metadata"]["summary"] == "Generated dependency summary"
    if not dry_run:
        result.assert_installed("dependency", editable=False)


@pytest.mark.parametrize("active_named_root", [False, True])
def test_only_deps_report_handles_root_also_used_as_direct_dependency(
    script: PipTestEnvironment, active_named_root: bool
) -> None:
    make_wheel(
        "leaf", "1.0", metadata_updates={"Summary": "Static leaf summary"}
    ).save_to_dir(script.scratch_path)
    backend_event = script.scratch_path / "shared-backend-imported"
    script.environ["PIP_TEST_SHARED_METADATA"] = str(backend_event)
    shared = create_basic_sdist_for_package(
        script,
        "shared",
        "1.0",
        extra_files={
            "PKG-INFO": (
                "Metadata-Version: 2.2\nName: shared\nVersion: 1.0\n"
                "Requires-Dist: leaf==1.0\nDynamic: Summary\n\n"
            ),
            "pyproject.toml": textwrap.dedent("""\
                [build-system]
                requires = []
                build-backend = "backend"
                backend-path = ["."]
                """),
            "backend.py": textwrap.dedent("""\
                import os
                from pathlib import Path

                Path(os.environ["PIP_TEST_SHARED_METADATA"]).touch()

                def prepare_metadata_for_build_wheel(
                    metadata_directory, config_settings=None
                ):
                    dist_info = Path(metadata_directory) / "shared-1.0.dist-info"
                    dist_info.mkdir()
                    (dist_info / "METADATA").write_text(
                        "Metadata-Version: 2.2\\nName: shared\\nVersion: 1.0\\n"
                        "Requires-Dist: leaf==1.0\\n"
                        "Summary: Generated shared summary\\n"
                    )
                    return dist_info.name
                """),
        },
    )
    root = create_basic_sdist_for_package(
        script,
        "root",
        "1.0",
        extra_files={
            "PKG-INFO": (
                "Metadata-Version: 2.2\nName: root\nVersion: 1.0\n"
                f"Requires-Dist: shared @ {shared.as_uri()}\nDynamic: Summary\n\n"
            ),
            "pyproject.toml": textwrap.dedent("""\
                [build-system]
                requires = []
                build-backend = "backend"
                backend-path = ["."]
                """),
            "backend.py": "raise RuntimeError('Excluded root backend ran')\n",
        },
    )
    named_root = "shared==1.0"
    if not active_named_root:
        named_root += '; python_version == "0"'

    script.pip(
        "install",
        "--only-deps",
        "--dry-run",
        "--ignore-installed",
        "--report",
        "report.json",
        "--no-index",
        "--find-links",
        script.scratch_path,
        root,
        named_root,
    )

    report = json.loads(script.scratch_path.joinpath("report.json").read_text())
    metadata = {
        item["metadata"]["name"]: item["metadata"] for item in report["install"]
    }
    assert metadata["leaf"]["summary"] == "Static leaf summary"
    if active_named_root:
        assert set(metadata) == {"leaf"}
        assert not backend_event.exists()
    else:
        assert set(metadata) == {"leaf", "shared"}
        assert metadata["shared"]["summary"] == "Generated shared summary"
        assert backend_event.exists()
