import json
from pathlib import Path

import pytest

from tests.lib import PipTestEnvironment, create_basic_sdist_for_package, make_wheel


def checked_prefix(
    script: PipTestEnvironment,
    *,
    metadata_version: str = "2.6",
    requires: str = "[]",
    isolated: bool = False,
) -> Path:
    backend = "backend" if isolated else "unavailable_backend"
    return create_basic_sdist_for_package(
        script,
        "checked_prefix",
        "2",
        extra_files={
            "PKG-INFO": (
                f"Metadata-Version: {metadata_version}\n"
                "Name: checked-prefix\nVersion: 2\n"
                "Requires-Dist: dependency==2\nDynamic: Requires-Dist\n"
                "Dynamic: Requires-Python\n"
            ),
            "pyproject.toml": (
                f'[build-system]\nrequires = {requires}\nbuild-backend = "{backend}"\n'
                'backend-path = ["."]\n'
            ),
            "backend.py": (
                "import os\nfrom pathlib import Path\n"
                "def get_requires_for_build_wheel(config_settings=None):\n"
                '    Path(os.environ["PIP_TEST_CHECKED_EVENTS"]).write_text(\n'
                '        "requirements"\n    )\n'
                "    return []\n"
                "def prepare_metadata_for_build_wheel(*args, **kwargs):\n"
                '    raise RuntimeError("Unexpected metadata hook")\n'
            ),
        },
    )


@pytest.mark.parametrize(
    "mode", ["fixed", "missing", "provisional", "compatible", "isolated"]
)
def test_dependency_preflight_preserves_requested_build_checks(
    script: PipTestEnvironment, mode: str
) -> None:
    events = script.scratch_path / "events"
    script.environ["PIP_TEST_CHECKED_EVENTS"] = str(events)
    archive = checked_prefix(
        script,
        metadata_version="2.5" if mode == "provisional" else "2.6",
        requires='["missing-build-requirement"]' if mode == "missing" else "[]",
        isolated=mode == "isolated",
    )
    for version in ("1", "2"):
        make_wheel("dependency", version).save_to_dir(script.scratch_path)
    isolation = [] if mode == "isolated" else ["--no-build-isolation"]
    result = script.pip(
        "download",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--check-build-dependencies",
        "--ignore-requires-python",
        *isolation,
        archive,
        "dependency==2" if mode == "compatible" else "dependency==1",
        expect_error=True,
    )
    if mode == "missing":
        assert "missing-build-requirement" in result.stderr
        assert "are missing" in result.stderr
    elif mode in {"fixed", "isolated"}:
        assert "ResolutionImpossible" in result.stderr
        assert "backend-unavailable" not in result.stderr
    else:
        assert "backend-unavailable" in result.stderr
    assert "Unexpected metadata hook" not in result.stderr
    if mode == "isolated":
        assert events.read_text() == "requirements"
    else:
        assert not events.exists()


def test_checked_report_backtracks_and_completes_selected_metadata(
    script: PipTestEnvironment,
) -> None:
    checked_prefix(script)
    complete = (
        "Metadata-Version: 2.6\nName: checked-prefix\nVersion: 1\n"
        "Requires-Python: >=3.10\nRequires-Dist: dependency==1\n"
        "Summary: selected complete metadata\n"
    )
    create_basic_sdist_for_package(
        script,
        "checked_prefix",
        "1",
        extra_files={
            "PKG-INFO": (
                "Metadata-Version: 2.6\nName: checked-prefix\nVersion: 1\n"
                "Requires-Python: >=3.10\nRequires-Dist: dependency==1\n"
                "Dynamic: Summary\n"
            ),
            "complete-metadata": complete,
            "pyproject.toml": (
                '[build-system]\nrequires = []\nbuild-backend = "backend"\n'
                'backend-path = ["."]\n'
            ),
            "backend.py": (
                "import os\nfrom pathlib import Path\n"
                "def prepare_metadata_for_build_wheel(\n"
                "    metadata_directory, config_settings=None\n):\n"
                '    Path(os.environ["PIP_TEST_CHECKED_EVENTS"]).write_text(\n'
                '        "selected metadata"\n    )\n'
                '    info = Path(metadata_directory) / "checked_prefix-1.dist-info"\n'
                "    info.mkdir()\n"
                '    (info / "METADATA").write_bytes(\n'
                '        Path("complete-metadata").read_bytes()\n    )\n'
                "    return info.name\n"
            ),
        },
    )
    dependency = make_wheel("dependency", "1").save_to_dir(script.scratch_path)
    events = script.scratch_path / "events"
    script.environ["PIP_TEST_CHECKED_EVENTS"] = str(events)
    script.pip(
        "install",
        "--dry-run",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--check-build-dependencies",
        "--no-build-isolation",
        "--report",
        "report.json",
        "checked-prefix",
        f"dependency @ {Path(dependency).as_uri()}",
    )
    report = json.loads(script.scratch_path.joinpath("report.json").read_text())
    packages = {
        entry["metadata"]["name"]: entry["metadata"] for entry in report["install"]
    }
    assert {name: metadata["version"] for name, metadata in packages.items()} == {
        "checked-prefix": "1",
        "dependency": "1",
    }
    assert packages["checked-prefix"]["summary"] == "selected complete metadata"
    assert events.read_text() == "selected metadata"
