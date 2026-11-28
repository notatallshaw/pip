import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tests.lib import PipTestEnvironment, TestPipResult, create_basic_wheel_for_package
from tests.lib.resolver import tracked_install
from tests.lib.wheel import make_wheel


def install_to_prefix(
    script: PipTestEnvironment,
    requirements: list[str],
    *,
    expect_error: bool = False,
) -> tuple[TestPipResult, dict[str, Any], Path]:
    """Keep each installation outside the next request's installed environment."""
    prefix = script.scratch_path / "prefix"
    report_path = script.scratch_path / "report.json"
    result = tracked_install(
        script,
        *requirements,
        options=("--prefix", str(prefix), "--no-compile", "--report", str(report_path)),
        ignore_installed=False,
        expect_error=expect_error,
    )
    if expect_error:
        return result, {}, prefix

    report = json.loads(report_path.read_text())
    return (
        result,
        {item["metadata"]["name"]: item for item in report["install"]},
        prefix,
    )


def installed_metadata(prefix: Path, name: str, version: str) -> Path:
    """Find one distribution's installed metadata across platform schemes."""
    matches = list(prefix.rglob(f"{name}-{version}.dist-info"))
    assert len(matches) == 1, matches
    return matches[0]


@pytest.mark.parametrize(
    "source_mode, root_extra",
    [("observed", True), ("unread", False), ("upfront", True)],
)
def test_source_discovery_preserves_url_provenance(
    script: PipTestEnvironment, source_mode: str, root_extra: bool
) -> None:
    dep = create_basic_wheel_for_package(script, "dep", "1.0")
    dependencies = [f"dep @ {dep.as_uri()}"]
    other_root = "bridge"
    expected = {"app": "2.0", "bridge": "1.0", "dep": "1.0"}
    missing = None
    bridge = None
    if source_mode != "observed":
        hidden = script.scratch_path / "hidden"
        hidden.mkdir()
        missing = create_basic_wheel_for_package(script, "missing", "3.0")
        missing = missing.rename(hidden / missing.name)
        bridge = Path(
            make_wheel(
                name="bridge",
                version="1.0",
                metadata_updates={
                    "Requires-Dist": [
                        f"missing @ {missing.as_uri()}",
                        f"dep @ {dep.as_uri()}",
                    ]
                },
            ).save_to_dir(script.scratch_path)
        )
        dependencies = [f"dep @ {dep.as_uri()}", "missing>=2", "bridge==1.0"]
        other_root = "shortcut"
        expected.update(missing="3.0", shortcut="1.0")
    other = create_basic_wheel_for_package(
        script, other_root, "1.0", depends=["dep>=1"]
    )
    for version in ("1.0", "2.0"):
        create_basic_wheel_for_package(
            script, "app", version, depends=dependencies, extras={"feature": []}
        )

    requirements = ["app[feature]" if root_extra else "app", other_root]
    if source_mode == "upfront":
        assert missing is not None
        requirements.append(f"missing @ {missing.as_uri()}")
    result, report, prefix = install_to_prefix(
        script, requirements, expect_error=source_mode == "unread"
    )

    # Missing's URL cannot rescue the solve before bridge metadata is read.
    if source_mode == "unread":
        assert missing is not None
        assert result.returncode == 1
        assert "ResolutionImpossible" in result.stderr
        assert "Traceback" not in result.stderr
        assert result.stdout.count("Nab source-domain restart:") == 1
        reads = [
            json.loads(line.partition(" ")[2])
            for line in result.stdout.splitlines()
            if line.startswith("DEPENDENCY_READ ")
        ]
        assert any(dep.as_uri() in requirement for requirement in reads)
        assert not any(missing.as_uri() in requirement for requirement in reads)
        assert "PREPARE bridge-1.0-" not in result.stdout
        assert not list(prefix.rglob("*.dist-info"))
        script.assert_not_installed("app", "dep", "missing", "bridge", "shortcut")
        return

    assert {
        name: item["metadata"]["version"] for name, item in report.items()
    } == expected
    assert report["dep"]["is_direct"] is True
    origin = installed_metadata(prefix, "dep", "1.0") / "direct_url.json"
    assert json.loads(origin.read_text())["url"] == dep.as_uri()

    assert report["app"]["requested"] is True
    assert (installed_metadata(prefix, "app", "2.0") / "REQUESTED").exists()
    if root_extra:
        assert report["app"]["requested_extras"] == ["feature"]
    assert result.stdout.splitlines().count("PREPARE app-2.0-py2.py3-none-any.whl") == 1
    assert result.stdout.count("Nab source-domain restart:") == 1

    expected_files = {
        "app": script.scratch_path / "app-2.0-py2.py3-none-any.whl",
        "dep": dep,
        other_root: other,
    }
    if source_mode == "upfront":
        assert missing is not None
        assert bridge is not None
        expected_files.update(missing=missing, bridge=bridge)
        assert report["missing"]["is_direct"] is True
    for name, artifact in expected_files.items():
        download = report[name]["download_info"]
        assert download["url"] == artifact.as_uri()
        assert (
            download["archive_info"]["hashes"]["sha256"]
            == hashlib.sha256(artifact.read_bytes()).hexdigest()
        )
    # Include the isolated prefix in distribution discovery for pip check.
    prefix_site = installed_metadata(prefix, "app", "2.0").parent
    script.environ["PYTHONPATH"] = str(prefix_site)
    script.pip("check")


def test_rejected_url_parent_does_not_change_ordinary_origin(
    script: PipTestEnvironment,
) -> None:
    dep = create_basic_wheel_for_package(script, "dep", "1.0", extras={"feature": []})
    create_basic_wheel_for_package(script, "app", "1.0", depends=["dep>=1"])
    make_wheel(
        name="app",
        version="2.0",
        metadata_updates={"Requires-Dist": [f"dep @ {dep.as_uri()}", "missing>=1"]},
    ).save_to_dir(script.scratch_path)

    _, report, prefix = install_to_prefix(script, ["dep[feature]==1.0", "app"])
    assert report["app"]["metadata"]["version"] == "1.0"
    assert report["dep"]["metadata"]["version"] == "1.0"
    assert report["dep"]["is_direct"] is False
    metadata = installed_metadata(prefix, "dep", "1.0")
    assert not (metadata / "direct_url.json").exists()

    assert report["dep"]["requested"] is True
    assert report["dep"]["requested_extras"] == ["feature"]
    assert (metadata / "REQUESTED").exists()
