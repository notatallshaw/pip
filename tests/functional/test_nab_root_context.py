import json

import pytest

from tests.lib import PipTestEnvironment, create_basic_wheel_for_package
from tests.lib.wheel import make_wheel


@pytest.mark.parametrize("late_url", [False, True])
def test_root_extras_keep_requested_metadata_after_transitive_preparation(
    script: PipTestEnvironment, late_url: bool
) -> None:
    for version in ("1.0", "2.0"):
        make_wheel(
            name="app",
            version=version,
            metadata=(
                f"Metadata-Version: 2.1\nName: app\nVersion: {version}\n"
                "Provides-Extra: feature\n"
            ),
        ).save_to_dir(script.scratch_path)
    create_basic_wheel_for_package(script, "bridge", "1.0", depends=["app==2.0"])
    requirements = ["app[feature]", "bridge"]
    if late_url:
        hidden = script.scratch_path / "hidden"
        hidden.mkdir()
        leaf = create_basic_wheel_for_package(script, "leaf", "1.0")
        leaf = leaf.rename(hidden / leaf.name)
        for version in ("1.0", "2.0", "3.0"):
            make_wheel(
                name="url_parent",
                version=version,
                metadata=(
                    f"Metadata-Version: 2.1\nName: url-parent\nVersion: {version}\n"
                    f"Requires-Dist: leaf @ {leaf.as_uri()}\n"
                ),
            ).save_to_dir(script.scratch_path)
        requirements.append("url-parent")

    report_path = script.scratch_path / "report.json"
    result = script.pip(
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--report",
        report_path,
        *requirements,
    )

    script.assert_installed(app="2.0", bridge="1.0")
    route = (
        "CatalogueUnsupported: URL dependency" if late_url else "Nab catalogue success"
    )
    assert route in result.stdout
    report = json.loads(report_path.read_text())
    app = next(item for item in report["install"] if item["metadata"]["name"] == "app")
    assert app["requested"] is True
    assert app["requested_extras"] == ["feature"]
    assert script.site_packages / "app-2.0.dist-info/REQUESTED" in result.files_created
