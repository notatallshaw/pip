from __future__ import annotations

import base64
import json
import textwrap

import pytest

from tests.lib import PipTestEnvironment, create_basic_sdist_for_package, make_wheel


@pytest.mark.parametrize(
    "scenario",
    [
        "install",
        "dry-run",
        "unparsed",
        "missing",
        "changed-fixed",
        "complete",
        "complete-ignored",
        "checked-rewrite",
        "build-failure",
    ],
)
def test_report_completes_only_selected_source_metadata(
    script: PipTestEnvironment, scenario: str
) -> None:
    make_wheel("dependency", "1.0").save_to_dir(script.scratch_path)
    metadata = (
        "Metadata-Version: 2.2\nName: package\nVersion: 1.0\n"
        "Requires-Dist: dependency==1.0\nSummary: Backend summary\n"
    )
    if scenario not in {"unparsed", "complete", "complete-ignored"}:
        metadata += "Dynamic: Summary\n"
    if scenario == "unparsed":
        metadata += "X-Unparsed: value\n"
    if scenario == "complete-ignored":
        metadata += "Requires-Python: >=99\nProvides-Extra: feature\n"
    wheel = make_wheel(
        "package",
        "1.0",
        metadata_updates={
            "Requires-Dist": (
                "dependency==2.0" if scenario == "changed-fixed" else "dependency==1.0"
            ),
            "Summary": "Backend summary",
        },
        extra_files={"package/__init__.py": ""},
    )
    files = {
        "pyproject.toml": (
            '[build-system]\nrequires = []\nbuild-backend = "backend"\n'
            'backend-path = ["."]\n'
        ),
        "wheel-data": base64.b64encode(wheel.as_bytes()).decode(),
        "backend.py": textwrap.dedent("""\
            import base64
            import os
            from io import BytesIO
            from pathlib import Path
            from zipfile import ZipFile

            def record(event):
                with open(os.environ["PIP_TEST_REPORT_EVENTS"], "a") as f:
                    f.write(event + "\\n")

            def get_requires_for_build_wheel(config_settings=None):
                record("requirements")
                if os.environ["PIP_TEST_REPORT_SCENARIO"] == "checked-rewrite":
                    metadata = Path("PKG-INFO")
                    contents = metadata.read_text().replace(
                        "dependency==1.0", "dependency==2.0"
                    ).replace("Dynamic: Summary\\n", "")
                    metadata.write_text(contents)
                return []

            def prepare_metadata_for_build_wheel(
                metadata_directory, config_settings=None
            ):
                record("metadata")
                if os.environ["PIP_TEST_REPORT_SCENARIO"] == "install":
                    raise AssertionError("Unexpected metadata hook")
                target = Path(metadata_directory) / "package-1.0.dist-info"
                target.mkdir()
                data = base64.b64decode(Path("wheel-data").read_bytes())
                with ZipFile(BytesIO(data)) as wheel:
                    contents = wheel.read(f"{target.name}/METADATA")
                (target / "METADATA").write_bytes(contents)
                return target.name

            def build_wheel(
                wheel_directory, config_settings=None, metadata_directory=None
            ):
                record("wheel")
                assert metadata_directory is None
                if os.environ["PIP_TEST_REPORT_SCENARIO"] == "build-failure":
                    raise RuntimeError("Requested build failure")
                filename = "package-1.0-py2.py3-none-any.whl"
                data = base64.b64decode(Path("wheel-data").read_bytes())
                (Path(wheel_directory) / filename).write_bytes(data)
                return filename
            """),
    }
    if scenario != "missing":
        files["PKG-INFO"] = metadata
    archive = create_basic_sdist_for_package(
        script, "package", "1.0", extra_files=files
    )
    script.environ["PIP_TEST_REPORT_EVENTS"] = str(script.scratch_path / "events")
    script.environ["PIP_TEST_REPORT_SCENARIO"] = scenario
    options = [] if scenario in {"install", "build-failure"} else ["--dry-run"]
    if scenario in {"missing", "complete-ignored"}:
        options += ["--no-deps", "--ignore-requires-python"]
    if scenario == "checked-rewrite":
        options += ["--check-build-dependencies"]
    failure = scenario in {"changed-fixed", "build-failure", "checked-rewrite"}
    result = script.pip(
        "install",
        "--no-index",
        "--no-cache-dir",
        "--find-links",
        script.scratch_path,
        "--report",
        "report.json",
        *options,
        archive,
        expect_error=failure,
    )

    events = script.scratch_path / "events"
    observed = events.read_text().splitlines() if events.exists() else []
    expected = (
        []
        if scenario in {"complete", "complete-ignored"}
        else [
            "requirements",
            "wheel" if scenario in {"install", "build-failure"} else "metadata",
        ]
    )
    if scenario == "checked-rewrite":
        expected = ["requirements"]
    assert observed == expected
    report_path = script.scratch_path / "report.json"
    if failure:
        assert not report_path.exists()
        assert (
            "invalid metadata"
            if scenario in {"changed-fixed", "checked-rewrite"}
            else "Requested build failure"
        ) in result.stderr
    else:
        report = json.loads(report_path.read_text())
        package = next(
            item for item in report["install"] if item["metadata"]["name"] == "package"
        )
        assert package["metadata"]["summary"] == "Backend summary"
        if scenario == "complete-ignored":
            assert package["metadata"]["requires_dist"] == ["dependency==1.0"]
            assert package["metadata"]["requires_python"] == ">=99"
            assert package["metadata"]["provides_extra"] == ["feature"]
            assert len(report["install"]) == 1
        if scenario == "install":
            result.assert_installed("package", editable=False)
