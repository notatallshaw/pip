from __future__ import annotations

import base64
import json
import sys
import textwrap

import pytest

from tests.lib import PipTestEnvironment, create_basic_sdist_for_package, make_wheel


@pytest.mark.parametrize(
    "metadata_kind, cross_target, incompatible_python, wheel_policy",
    [
        ("static", False, False, None),
        ("static", True, False, None),
        ("static", False, True, None),
        ("missing", False, False, None),
        ("dynamic", False, False, None),
        ("dynamic", True, False, None),
        ("dynamic", True, False, "host"),
        ("dynamic", True, False, "foreign"),
        ("dynamic", True, False, "no-binary"),
    ],
)
def test_inprocess_build_dependencies_use_required_static_metadata(
    script: PipTestEnvironment,
    metadata_kind: str,
    cross_target: bool,
    incompatible_python: bool,
    wheel_policy: str | None,
) -> None:
    events = script.scratch_path / "build-events"
    script.environ["PIP_TEST_INPROCESS_EVENTS"] = str(events)
    make_wheel(
        "runtime_dependency", "1.0", extra_files={"runtime_dependency/__init__.py": ""}
    ).save_to_dir(script.scratch_path)
    requires_python = (
        ">=99"
        if incompatible_python
        else f">={sys.version_info.major}.{sys.version_info.minor}"
    )
    metadata = (
        "Metadata-Version: 2.2\nName: root-backend\nVersion: 1.0\n"
        f"Requires-Python: {requires_python}\n"
        "Requires-Dist: runtime-dependency==1.0\n"
    )
    backend_module = textwrap.dedent("""\
        import os
        from pathlib import Path

        import runtime_dependency

        def record(event):
            with open(os.environ["PIP_TEST_INPROCESS_EVENTS"], "a") as f:
                f.write(event + "\\n")

        def get_requires_for_build_wheel(config_settings=None):
            record("root requirements")
            return []

        def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
            record("root metadata")
            dist = Path(metadata_directory) / "project-1.0.dist-info"
            dist.mkdir()
            (dist / "METADATA").write_text(
                "Metadata-Version: 2.2\\nName: project\\nVersion: 1.0\\n"
            )
            return dist.name
        """)
    wheel = make_wheel(
        "root_backend",
        "1.0",
        metadata=metadata,
        extra_files={"root_backend.py": backend_module},
    )
    if wheel_policy is not None:
        python_tag = (
            "cp39"
            if wheel_policy == "foreign"
            else f"cp{sys.version_info.major}{sys.version_info.minor}"
        )
        make_wheel(
            "root_backend",
            "1.0",
            metadata=metadata,
            wheel_metadata_updates={"Tag": f"{python_tag}-none-any"},
            extra_files={"root_backend.py": backend_module},
        ).save_to(script.scratch_path / f"root_backend-1.0-{python_tag}-none-any.whl")
    files = {
        "pyproject.toml": textwrap.dedent("""\
            [build-system]
            requires = []
            build-backend = "dependency_builder"
            backend-path = ["."]
            """),
        "wheel-data": base64.b64encode(wheel.as_bytes()).decode(),
        "dependency_builder.py": textwrap.dedent("""\
            import base64
            import os
            from pathlib import Path

            def record(event):
                with open(os.environ["PIP_TEST_INPROCESS_EVENTS"], "a") as f:
                    f.write(event + "\\n")

            def get_requires_for_build_wheel(config_settings=None):
                record("dependency requirements")
                return []

            def prepare_metadata_for_build_wheel(*args, **kwargs):
                record("dependency metadata")
                raise RuntimeError("build dependency metadata hook executed")

            def build_wheel(
                wheel_directory, config_settings=None, metadata_directory=None
            ):
                assert metadata_directory is None
                record("dependency wheel")
                filename = "root_backend-1.0-py2.py3-none-any.whl"
                wheel_bytes = base64.b64decode(Path("wheel-data").read_bytes())
                (Path(wheel_directory) / filename).write_bytes(wheel_bytes)
                return filename
            """),
    }
    if metadata_kind != "missing":
        files["PKG-INFO"] = metadata + (
            "Dynamic: Requires-Dist\n" if metadata_kind == "dynamic" else ""
        )
    create_basic_sdist_for_package(script, "root_backend", "1.0", extra_files=files)
    project = script.scratch_path / "project"
    project.mkdir()
    project.joinpath("pyproject.toml").write_text(textwrap.dedent("""\
        [build-system]
        requires = ["root-backend==1.0"]
        build-backend = "root_backend"
        """))
    target_options = ["--python-version", "3.9"] if cross_target else []
    if wheel_policy == "no-binary":
        target_options.append("--no-binary=root-backend")
    failure = (
        metadata_kind != "static" and wheel_policy != "host"
    ) or incompatible_python

    result = script.pip(
        "install",
        "--dry-run",
        "--ignore-installed",
        "--no-deps",
        "--ignore-requires-python",
        "--no-index",
        "--no-cache-dir",
        "--find-links",
        script.scratch_path,
        "--report",
        "report.json",
        "--use-feature=inprocess-build-deps",
        *target_options,
        project,
        expect_error=failure,
    )

    if incompatible_python:
        assert "requires a different Python" in result.stderr
        assert not events.exists()
    elif failure:
        assert events.read_text().splitlines() == [
            "dependency requirements",
            "dependency metadata",
        ]
    else:
        expected = ["root requirements", "root metadata"]
        if wheel_policy != "host":
            expected[:0] = ["dependency requirements", "dependency wheel"]
        assert events.read_text().splitlines() == expected
        report = json.loads(script.scratch_path.joinpath("report.json").read_text())
        assert [item["metadata"]["name"] for item in report["install"]] == ["project"]
