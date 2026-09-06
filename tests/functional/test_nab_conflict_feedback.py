import json

from pip._vendor.packaging.utils import canonicalize_name

from tests.lib import PipTestEnvironment, create_basic_wheel_for_package


def test_repeated_conflicts_prioritize_model_constraints(
    script: PipTestEnvironment,
) -> None:
    for version in ("1", "2"):
        create_basic_wheel_for_package(script, "hub", version)
    for version in range(1, 17):
        create_basic_wheel_for_package(
            script, "model", str(version), depends=["hub<2"]
        )

    script.environ["PIP_RESOLVER_DEBUG"] = "1"
    report_path = script.scratch_path / "report.json"
    result = script.pip(
        "install",
        "--dry-run",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--report",
        report_path,
        "hub",
        "model",
    )
    report = json.loads(report_path.read_text())
    selected = {
        canonicalize_name(item["metadata"]["name"]): item["metadata"]["version"]
        for item in report["install"]
    }
    assert selected == {"hub": "1", "model": "16"}

    pinning = result.stdout.count("Reporter.pinning(")
    assert 0 < pinning < 16
