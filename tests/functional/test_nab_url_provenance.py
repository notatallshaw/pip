import json
from pathlib import Path
from typing import Any

import pytest

from tests.lib import PipTestEnvironment, TestPipResult, create_basic_wheel_for_package
from tests.lib.wheel import make_wheel


def install_to_prefix(
    script: PipTestEnvironment, label: str, requirements: list[str]
) -> tuple[TestPipResult, dict[str, Any], Path]:
    """Keep each installation outside the next request's installed environment."""
    prefix = script.scratch_path / f"{label}-prefix"
    report_path = script.scratch_path / f"{label}-report.json"
    result = script.pip(
        "install",
        *(("--ignore-installed",) if label == "fast" else ()),
        "--prefix",
        prefix,
        "--no-compile",
        "--report",
        report_path,
        "--no-index",
        "--find-links",
        script.scratch_path,
        *requirements,
    )
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


@pytest.mark.parametrize("hidden_url", [False, True])
@pytest.mark.parametrize("root_extra", [False, True])
def test_url_fallback_preserves_origin_after_catalogue_preparation(
    script: PipTestEnvironment, hidden_url: bool, root_extra: bool
) -> None:
    dep = create_basic_wheel_for_package(script, "dep", "1.0")
    dependencies = [f"dep @ {dep.as_uri()}"]
    other_root = "bridge"
    expected = {"app": "2.0", "bridge": "1.0", "dep": "1.0"}
    if hidden_url:
        hidden = script.scratch_path / "hidden"
        hidden.mkdir()
        missing = create_basic_wheel_for_package(script, "missing", "3.0")
        missing = missing.rename(hidden / missing.name)
        make_wheel(
            name="bridge",
            version="1.0",
            metadata=(
                "Metadata-Version: 2.1\nName: bridge\nVersion: 1.0\n"
                f"Requires-Dist: missing @ {missing.as_uri()}\n"
                f"Requires-Dist: dep @ {dep.as_uri()}\n"
            ),
        ).save_to_dir(script.scratch_path)
        dependencies = ["missing>=2", "bridge==1.0"]
        other_root = "shortcut"
        expected.update(missing="3.0", shortcut="1.0")
    create_basic_wheel_for_package(script, other_root, "1.0", depends=["dep>=1"])
    for version in ("1.0", "2.0"):
        metadata = (
            f"Metadata-Version: 2.1\nName: app\nVersion: {version}\n"
            "Provides-Extra: feature\n"
        )
        metadata += "".join(f"Requires-Dist: {dep}\n" for dep in dependencies)
        make_wheel(name="app", version=version, metadata=metadata).save_to_dir(
            script.scratch_path
        )

    requirements = ["app[feature]" if root_extra else "app", other_root]
    for label in ("native", "fast"):
        script.assert_not_installed("app", "bridge", "dep")
        result, report, prefix = install_to_prefix(script, label, requirements)
        assert {
            name: item["metadata"]["version"] for name, item in report.items()
        } == expected

        assert report["dep"]["is_direct"] is True, label
        origin = installed_metadata(prefix, "dep", "1.0") / "direct_url.json"
        assert json.loads(origin.read_text())["url"] == dep.as_uri(), label

        assert report["app"]["requested"] is True, label
        assert (installed_metadata(prefix, "app", "2.0") / "REQUESTED").exists()
        if root_extra:
            assert report["app"]["requested_extras"] == ["feature"], label

        retry = "Nab native retry: URL requires fresh preparation"
        if label == "native":
            assert retry not in result.stdout
            continue
        assert result.stdout.count(retry) == 1
        if hidden_url:
            assert "Nab catalogue fallback: ResolutionError" in result.stdout
        else:
            assert "CatalogueUnsupported: URL dependency" in result.stdout


def test_rejected_url_parent_does_not_change_ordinary_origin(
    script: PipTestEnvironment,
) -> None:
    dep = create_basic_wheel_for_package(script, "dep", "1.0", extras={"feature": []})
    create_basic_wheel_for_package(script, "app", "1.0", depends=["dep>=1"])
    make_wheel(
        name="app",
        version="2.0",
        metadata=(
            "Metadata-Version: 2.1\nName: app\nVersion: 2.0\n"
            f"Requires-Dist: dep @ {dep.as_uri()}\n"
            "Requires-Dist: missing>=1\n"
        ),
    ).save_to_dir(script.scratch_path)

    for label in ("native", "fast"):
        result, report, prefix = install_to_prefix(
            script, label, ["dep[feature]==1.0", "app"]
        )
        assert report["app"]["metadata"]["version"] == "1.0"
        assert report["dep"]["metadata"]["version"] == "1.0"
        assert report["dep"]["is_direct"] is False, label
        metadata = installed_metadata(prefix, "dep", "1.0")
        assert not (metadata / "direct_url.json").exists(), label

        assert report["dep"]["requested"] is True
        assert report["dep"]["requested_extras"] == ["feature"]
        assert (metadata / "REQUESTED").exists()
        if label == "fast":
            assert (
                result.stdout.count("Nab native retry: URL requires fresh preparation")
                == 1
            )
