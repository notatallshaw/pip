import json

from pip._vendor.packaging.utils import canonicalize_name

from tests.lib import PipTestEnvironment, create_basic_wheel_for_package


def test_exhausted_query_feedback_limits_unrelated_backtracking(
    script: PipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(
        script,
        "datacontract-cli",
        "0.10.10",
        depends=[
            "fastparquet==2024.5.0",
            "soda-core-duckdb<3.4.0,>=3.3.1",
            "opentelemetry-exporter-otlp-proto-grpc~=1.16",
            "opentelemetry-exporter-otlp-proto-http~=1.16",
        ],
    )
    create_basic_wheel_for_package(
        script, "fastparquet", "2024.5.0", depends=["fsspec", "packaging"]
    )
    create_basic_wheel_for_package(
        script, "soda-core-duckdb", "3.3.22", depends=["soda-core==3.3.22"]
    )
    create_basic_wheel_for_package(
        script,
        "soda-core",
        "3.3.22",
        depends=[
            "opentelemetry-api<1.23.0,>=1.16.0",
            "opentelemetry-exporter-otlp-proto-http<1.23.0,>=1.16.0",
        ],
    )

    for version in ("1.34.0", "1.22.0"):
        create_basic_wheel_for_package(script, "opentelemetry-api", version)
        create_basic_wheel_for_package(
            script,
            "opentelemetry-sdk",
            version,
            depends=[f"opentelemetry-api=={version}"],
        )
        create_basic_wheel_for_package(
            script, "opentelemetry-exporter-otlp-proto-common", version
        )
        for protocol in ("http", "grpc"):
            create_basic_wheel_for_package(
                script,
                f"opentelemetry-exporter-otlp-proto-{protocol}",
                version,
                depends=[
                    "opentelemetry-api~=1.15",
                    f"opentelemetry-exporter-otlp-proto-common=={version}",
                    f"opentelemetry-sdk~={version}",
                ],
            )

    for version in ("25.0", "24.2", "24.1"):
        create_basic_wheel_for_package(script, "packaging", version)
    for version in ("2025.5.1", "2025.5.0", "2025.3.2"):
        create_basic_wheel_for_package(script, "fsspec", version)

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
        "datacontract-cli==0.10.10",
    )
    report = json.loads(report_path.read_text())
    selected = {
        canonicalize_name(item["metadata"]["name"]): item["metadata"]["version"]
        for item in report["install"]
    }
    assert selected == {
        "datacontract-cli": "0.10.10",
        "fastparquet": "2024.5.0",
        "soda-core-duckdb": "3.3.22",
        "soda-core": "3.3.22",
        "opentelemetry-api": "1.22.0",
        "opentelemetry-sdk": "1.22.0",
        "opentelemetry-exporter-otlp-proto-common": "1.22.0",
        "opentelemetry-exporter-otlp-proto-http": "1.22.0",
        "opentelemetry-exporter-otlp-proto-grpc": "1.22.0",
        "packaging": "25.0",
        "fsspec": "2025.5.1",
    }
    assert 0 < result.stdout.count("Reporter.pinning(") < 100
