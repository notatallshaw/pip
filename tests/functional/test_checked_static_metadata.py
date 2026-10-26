from __future__ import annotations

import base64
import textwrap
from pathlib import Path

import pytest

from tests.lib import PipTestEnvironment, create_basic_sdist_for_package, make_wheel


@pytest.mark.parametrize(
    "mode", ["no-isolation", "missing", "isolated", "build", "dynamic"]
)
def test_build_dependency_checks_do_not_require_static_metadata_hooks(
    script: PipTestEnvironment, mode: str
) -> None:
    events = script.scratch_path / "backend-events"
    source_metadata = "Metadata-Version: 2.2\nName: check-demo\nVersion: 1.0\n"
    if mode == "dynamic":
        source_metadata += "Dynamic: Requires-Dist\n"
    requires = '["missing-build-requirement"]' if mode == "missing" else "[]"
    backend = (
        "unavailable_backend" if mode in {"no-isolation", "missing"} else "backend"
    )
    wheel = make_wheel("check_demo", "1.0", metadata=source_metadata)
    archive = create_basic_sdist_for_package(
        script,
        "check_demo",
        "1.0",
        extra_files={
            "PKG-INFO": source_metadata,
            "wheel-data": base64.b64encode(wheel.as_bytes()).decode(),
            "pyproject.toml": (
                f'[build-system]\nrequires = {requires}\nbuild-backend = "{backend}"\n'
                'backend-path = ["."]\n'
            ),
            "backend.py": textwrap.dedent(f"""\
                import base64
                from pathlib import Path

                def record(event):
                    with open({str(events)!r}, "a") as f:
                        f.write(event + "\\n")

                def get_requires_for_build_wheel(config_settings=None):
                    record("requirements")
                    return []

                def prepare_metadata_for_build_wheel(*args, **kwargs):
                    record("metadata")
                    raise RuntimeError("metadata hook executed")

                def build_wheel(
                    wheel_directory, config_settings=None, metadata_directory=None
                ):
                    assert metadata_directory is None
                    record("wheel")
                    filename = "check_demo-1.0-py2.py3-none-any.whl"
                    wheel_bytes = base64.b64decode(Path("wheel-data").read_bytes())
                    (Path(wheel_directory) / filename).write_bytes(wheel_bytes)
                    return filename
                """),
        },
    )
    isolation_options = (
        ["--no-build-isolation"] if mode in {"no-isolation", "missing"} else []
    )
    failure = mode in {"missing", "dynamic"}
    destination_option = "--wheel-dir" if mode == "build" else "--dest"

    result = script.pip(
        "wheel" if mode == "build" else "download",
        "--check-build-dependencies",
        "--no-index",
        destination_option,
        "downloaded",
        *isolation_options,
        archive,
        expect_error=failure,
    )

    if mode == "missing":
        assert "missing-build-requirement" in result.stderr
        assert not events.exists()
    elif mode == "dynamic":
        assert events.read_text().splitlines() == ["requirements", "metadata"]
    else:
        filename = (
            "check_demo-1.0-py2.py3-none-any.whl" if mode == "build" else archive.name
        )
        result.did_create(Path("scratch/downloaded") / filename)
        if mode == "build":
            assert events.read_text().splitlines() == ["requirements", "wheel"]
        elif mode == "isolated":
            assert events.read_text().splitlines() == ["requirements"]
        else:
            assert not events.exists()
