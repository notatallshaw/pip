from __future__ import annotations

import base64
import textwrap

import pytest

from tests.lib import (
    PipTestEnvironment,
    create_basic_sdist_for_package,
    create_basic_wheel_for_package,
    make_wheel,
)


@pytest.mark.parametrize("command", ["install", "wheel"])
def test_static_sdist_build_without_metadata_hook(
    script: PipTestEnvironment, command: str
) -> None:
    events = script.scratch_path / "build-events"
    script.environ["PIP_TEST_BUILD_EVENTS"] = str(events)
    create_basic_wheel_for_package(script, "build_dependency", "1.0")
    wheel = make_wheel(
        "package",
        "1.0",
        metadata_updates={"Metadata-Version": "2.2", "Summary": "Built summary"},
        extra_files={"package/__init__.py": ""},
    )
    archive = create_basic_sdist_for_package(
        script,
        "package",
        "1.0",
        extra_files={
            "PKG-INFO": (
                "Metadata-Version: 2.2\nName: package\nVersion: 1.0\n"
                "Dynamic: Summary\n\n"
            ),
            "wheel-data": base64.b64encode(wheel.as_bytes()).decode(),
            "pyproject.toml": textwrap.dedent("""\
                [build-system]
                requires = []
                build-backend = "backend"
                backend-path = ["."]
                """),
            "backend.py": textwrap.dedent("""\
                import base64
                import os
                from pathlib import Path

                def record(event):
                    with open(os.environ["PIP_TEST_BUILD_EVENTS"], "a") as f:
                        f.write(event + "\\n")

                def get_requires_for_build_wheel(config_settings=None):
                    record("requirements")
                    return ["build_dependency==1.0"]

                def prepare_metadata_for_build_wheel(*args, **kwargs):
                    raise AssertionError("Unexpected metadata hook")

                def build_wheel(
                    wheel_directory, config_settings=None, metadata_directory=None
                ):
                    import build_dependency
                    assert metadata_directory is None
                    record("wheel")
                    filename = "package-1.0-py2.py3-none-any.whl"
                    wheel_bytes = base64.b64decode(Path("wheel-data").read_bytes())
                    (Path(wheel_directory) / filename).write_bytes(wheel_bytes)
                    return filename
                """),
        },
    )

    result = script.pip(
        command,
        "--no-index",
        "--no-cache-dir",
        "--find-links",
        script.scratch_path,
        archive,
    )

    assert events.read_text().splitlines() == ["requirements", "wheel"]
    if command == "install":
        result.assert_installed("package", editable=False)
    else:
        result.did_create(script.scratch / "package-1.0-py2.py3-none-any.whl")


@pytest.mark.parametrize("command", ["install", "wheel"])
def test_only_deps_avoids_static_sdist_backend(
    script: PipTestEnvironment, command: str
) -> None:
    create_basic_wheel_for_package(script, "dependency", "1.0")
    archive = create_basic_sdist_for_package(
        script,
        "package",
        "1.0",
        extra_files={
            "PKG-INFO": (
                "Metadata-Version: 2.2\nName: package\nVersion: 1.0\n"
                "Requires-Dist: dependency==1.0\n\n"
            ),
            "pyproject.toml": textwrap.dedent("""\
                [build-system]
                requires = ["absent_backend_dependency"]
                build-backend = "backend"
                backend-path = ["."]
                """),
            "backend.py": "raise AssertionError('Unexpected backend import')\n",
        },
    )

    output_options = ["--wheel-dir", "wheels"] if command == "wheel" else []
    result = script.pip(
        command,
        "--only-deps",
        "--no-index",
        "--find-links",
        script.scratch_path,
        *output_options,
        archive,
    )

    if command == "install":
        result.assert_installed("dependency", editable=False)
    else:
        result.did_create(script.scratch / "wheels/dependency-1.0-py2.py3-none-any.whl")
        assert not (
            script.scratch_path / "wheels/package-1.0-py2.py3-none-any.whl"
        ).exists()


@pytest.mark.parametrize("command", ["install", "wheel"])
@pytest.mark.parametrize("invalid_dependencies", [False, True])
def test_filename_identity_builds_without_metadata_preparation(
    script: PipTestEnvironment, command: str, invalid_dependencies: bool
) -> None:
    metadata = "Metadata-Version: 2.2\nName: package\nVersion: 1.0\n"
    if invalid_dependencies:
        metadata += "Requires-Dist: dependency>=broken\n"
    wheel = make_wheel(
        "package", "1.0", metadata=metadata, extra_files={"package/__init__.py": ""}
    )
    archive = create_basic_sdist_for_package(
        script,
        "package",
        "1.0",
        extra_files={
            "PKG-INFO": "Metadata-Version: 2.1\nName: other\nVersion: broken\n",
            "wheel-data": base64.b64encode(wheel.as_bytes()).decode(),
            "pyproject.toml": textwrap.dedent("""\
                [build-system]
                requires = []
                build-backend = "backend"
                backend-path = ["."]
                """),
            "backend.py": textwrap.dedent("""\
                import base64
                from pathlib import Path

                def prepare_metadata_for_build_wheel(*args, **kwargs):
                    raise AssertionError("Unexpected metadata hook")

                def build_wheel(
                    wheel_directory, config_settings=None, metadata_directory=None
                ):
                    assert metadata_directory is None
                    filename = "package-1.0-py2.py3-none-any.whl"
                    wheel_bytes = base64.b64decode(Path("wheel-data").read_bytes())
                    (Path(wheel_directory) / filename).write_bytes(wheel_bytes)
                    return filename
                """),
        },
    )

    result = script.pip(
        command,
        "--no-index",
        "--no-cache-dir",
        "--no-deps",
        "--ignore-requires-python",
        archive,
        expect_error=invalid_dependencies,
    )

    if invalid_dependencies:
        assert "Invalid Requires-Dist" in result.stderr
        assert "Unexpected metadata hook" not in result.stderr
    elif command == "install":
        result.assert_installed("package", editable=False)
    else:
        result.did_create(script.scratch / "package-1.0-py2.py3-none-any.whl")
