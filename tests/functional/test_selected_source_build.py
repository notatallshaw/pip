from __future__ import annotations

import base64
import json
import textwrap
from pathlib import Path

import pytest

from tests.lib import PipTestEnvironment, create_basic_sdist_for_package, make_wheel


def make_source(script: PipTestEnvironment, label: str) -> Path:
    metadata = "Metadata-Version: 2.1\nName: package\nVersion: 1.0\n\n"
    wheel = make_wheel(
        "package",
        "1.0",
        metadata=metadata,
        extra_files={"package/__init__.py": f"SOURCE = {label!r}\n"},
    )
    archive = create_basic_sdist_for_package(
        script,
        "package",
        "1.0",
        extra_files={
            "PKG-INFO": metadata,
            "backend-metadata": metadata,
            "wheel-data": base64.b64encode(wheel.as_bytes()).decode(),
            "pyproject.toml": textwrap.dedent("""\
                [build-system]
                requires = []
                build-backend = "backend"
                backend-path = ["."]
                """),
            "backend.py": textwrap.dedent(f"""\
                import base64
                import os
                from pathlib import Path

                def record(event):
                    with open(os.environ["PIP_TEST_BUILD_EVENTS"], "a") as f:
                        f.write({label!r} + ":" + event + "\\n")

                def prepare_metadata_for_build_wheel(
                    metadata_directory, config_settings=None
                ):
                    record("metadata")
                    dist_info = Path(metadata_directory) / "package-1.0.dist-info"
                    dist_info.mkdir()
                    (dist_info / "METADATA").write_bytes(
                        Path("backend-metadata").read_bytes()
                    )
                    return dist_info.name

                def build_wheel(
                    wheel_directory, config_settings=None, metadata_directory=None
                ):
                    record("wheel")
                    filename = "package-1.0-py2.py3-none-any.whl"
                    wheel_bytes = base64.b64decode(Path("wheel-data").read_bytes())
                    (Path(wheel_directory) / filename).write_bytes(wheel_bytes)
                    return filename
                """),
        },
    )
    directory = script.scratch_path / label
    directory.mkdir()
    return archive.rename(directory / archive.name)


@pytest.mark.parametrize("named_alias", [False, True])
@pytest.mark.parametrize("command", ["install", "wheel"])
def test_legacy_builds_and_reports_only_selected_sources(
    script: PipTestEnvironment, named_alias: bool, command: str
) -> None:
    first = make_source(script, "first")
    roots: list[str | Path]
    expected_events = ["first:metadata"]
    if named_alias:
        roots = [f"requested-name @ {first.as_uri()}"]
    else:
        second = make_source(script, "second")
        roots = [first, second]
        expected_events.append("second:metadata")
    expected_events.append("first:wheel")
    events = script.scratch_path / "build-events"
    script.environ["PIP_TEST_BUILD_EVENTS"] = str(events)
    output_options = (
        ["--report", "report.json"]
        if command == "install"
        else ["--wheel-dir", "wheels"]
    )

    result = script.pip(
        command,
        "--use-deprecated=legacy-resolver",
        "--no-index",
        "--no-cache-dir",
        "--no-deps",
        *output_options,
        *roots,
        allow_stderr_warning=True,
    )

    assert events.read_text().splitlines() == expected_events
    if command == "install":
        report = json.loads(script.scratch_path.joinpath("report.json").read_text())
        assert len(report["install"]) == 1
        assert report["install"][0]["download_info"]["url"] == first.as_uri()
        result.assert_installed("package", editable=False)
        script.run("python", "-c", "import package; assert package.SOURCE == 'first'")
    else:
        result.did_create(Path("scratch/wheels/package-1.0-py2.py3-none-any.whl"))
