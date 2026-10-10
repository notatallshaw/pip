import json

from pip._vendor.packaging.utils import canonicalize_name

from tests.lib import PipTestEnvironment, create_basic_wheel_for_package


def test_new_dependency_pin_precedes_unconstrained_root(
    script: PipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(
        script,
        "opentelemetry-instrumentation-threading",
        "0.50b0",
        depends=[
            "opentelemetry-api~=1.12",
            "opentelemetry-instrumentation==0.50b0",
        ],
    )
    create_basic_wheel_for_package(
        script,
        "opentelemetry-instrumentation",
        "0.50b0",
        depends=[
            "opentelemetry-api~=1.4",
            "opentelemetry-semantic-conventions==0.50b0",
        ],
    )
    for conventions, api in (("0.53b1", "1.32.1"), ("0.50b0", "1.29.0")):
        create_basic_wheel_for_package(
            script,
            "opentelemetry-semantic-conventions",
            conventions,
            depends=[f"opentelemetry-api=={api}"],
        )
        create_basic_wheel_for_package(script, "opentelemetry-api", api)

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
        "opentelemetry-instrumentation-threading==0.50b0",
        "opentelemetry-semantic-conventions",
    )
    report = json.loads(report_path.read_text())
    selected = {
        canonicalize_name(item["metadata"]["name"]): item["metadata"]["version"]
        for item in report["install"]
    }
    assert selected == {
        "opentelemetry-instrumentation-threading": "0.50b0",
        "opentelemetry-instrumentation": "0.50b0",
        "opentelemetry-semantic-conventions": "0.50b0",
        "opentelemetry-api": "1.29.0",
    }

    pinning = [
        line for line in result.stdout.splitlines() if "Reporter.pinning(" in line
    ]
    assert all(
        "opentelemetry_semantic_conventions-0.53b1" not in line for line in pinning
    ), pinning
    assert 0 < len(pinning) <= 6
