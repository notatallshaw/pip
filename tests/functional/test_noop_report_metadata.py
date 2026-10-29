from __future__ import annotations

import json

import pytest

from tests.lib import PipTestEnvironment, create_basic_sdist_for_package, make_wheel
from tests.lib.server import file_response, make_mock_server, server_running


@pytest.mark.parametrize(
    "scenario, dry_run",
    [
        ("noop", True),
        ("new-version", True),
        ("force", True),
        ("ignore-installed", True),
        ("file", True),
        ("dynamic-dependencies", True),
        ("dynamic-no-deps", True),
        ("missing-metadata", True),
        ("missing-metadata-python-check", True),
        ("missing-metadata-new-version", True),
        ("installed-editable", True),
        ("noop", False),
        ("dynamic-no-deps", False),
        ("missing-metadata", False),
    ],
)
def test_noop_report_requires_only_resolution_metadata(
    script: PipTestEnvironment, scenario: str, dry_run: bool
) -> None:
    installed = make_wheel(
        "report_demo",
        "1.0",
        extra_files={"report_demo/__init__.py": "SOURCE = 'installed'\n"},
    ).save_to_dir(script.scratch_path)
    script.pip("install", "--no-index", installed)
    if scenario == "installed-editable":
        direct_url = (
            script.site_packages_path / "report_demo-1.0.dist-info/direct_url.json"
        )
        direct_url.write_text(
            json.dumps(
                {"url": script.scratch_path.as_uri(), "dir_info": {"editable": True}}
            )
        )
    make_wheel("new_dependency", "1.0").save_to_dir(script.scratch_path)
    version = (
        "2.0" if scenario in {"new-version", "missing-metadata-new-version"} else "1.0"
    )
    metadata = (
        f"Metadata-Version: 2.2\nName: report-demo\nVersion: {version}\n"
        "Requires-Dist: new-dependency==1.0\nDynamic: Summary\n"
    )
    if scenario in {"dynamic-dependencies", "dynamic-no-deps"}:
        metadata += "Dynamic: Requires-Dist\n"
    files = {
        "pyproject.toml": (
            '[build-system]\nrequires = []\nbuild-backend = "unavailable_backend"\n'
        )
    }
    if not scenario.startswith("missing-metadata"):
        files["PKG-INFO"] = metadata
    archive = create_basic_sdist_for_package(
        script, "report_demo", version, extra_files=files
    )
    options = []
    if scenario == "force":
        options.append("--force-reinstall")
    elif scenario == "ignore-installed":
        options.append("--ignore-installed")
    elif scenario == "dynamic-no-deps":
        options.append("--no-deps")
    elif scenario == "missing-metadata-python-check":
        options.append("--no-deps")
    elif scenario.startswith("missing-metadata"):
        options.extend(["--no-deps", "--ignore-requires-python"])
    failure = scenario not in {"noop", "dynamic-no-deps", "missing-metadata"}
    server = make_mock_server()
    server.mock.return_value = file_response(archive)

    with server_running(server):
        url = (
            archive.as_uri()
            if scenario == "file"
            else f"http://localhost:{server.server_port}/{archive.name}"
        )
        result = script.pip(
            "install",
            *(["--dry-run"] if dry_run else []),
            "--no-index",
            "--no-cache-dir",
            "--find-links",
            script.scratch_path,
            "--report",
            "report.json",
            *options,
            url,
            expect_error=failure,
        )

    if failure:
        assert "backend-unavailable" in result.stderr
    else:
        report = json.loads(script.scratch_path.joinpath("report.json").read_text())
        names = [item["metadata"]["name"] for item in report["install"]]
        assert names == (
            []
            if scenario in {"dynamic-no-deps", "missing-metadata"}
            else ["new_dependency"]
        )
        script.run(
            "python",
            "-c",
            "import report_demo; assert report_demo.SOURCE == 'installed'",
        )
        if not dry_run and scenario == "noop":
            script.run(
                "python",
                "-c",
                "from importlib.metadata import version; "
                "assert version('new-dependency') == '1.0'",
            )
