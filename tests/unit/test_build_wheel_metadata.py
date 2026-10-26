from pathlib import Path

import pytest

from pip._vendor.pyproject_hooks import BuildBackendHookCaller

from pip._internal.exceptions import (
    InstallationError,
    InvalidWheelFilename,
    UnsupportedWheel,
)
from pip._internal.operations.build.wheel import verify_built_wheel
from pip._internal.req import req_install
from pip._internal.req.constructors import install_req_from_line
from pip._internal.utils.temp_dir import global_tempdir_manager

from tests.lib import make_wheel


@pytest.mark.parametrize("expected_name", [None, "package", "PACKAGE"])
@pytest.mark.parametrize("verify", [False, True])
def test_verified_built_metadata(
    tmp_path: Path, expected_name: str | None, verify: bool
) -> None:
    path = make_wheel("package", "1.0").save_to_dir(tmp_path)
    dist = verify_built_wheel(path, expected_name, verify=verify)
    assert dist.raw_name == "package"
    assert str(dist.version) == "1.0"


@pytest.mark.parametrize(
    "metadata, error",
    [
        ("Metadata-Version: 2.2\nVersion: 1.0\n", UnsupportedWheel),
        ("Metadata-Version: 2.2\nName: package\n", UnsupportedWheel),
        ("Metadata-Version: 2.2\nName: package\nVersion: broken\n", UnsupportedWheel),
        (
            "Metadata-Version: 2.2\nName: package\nName: package\nVersion: 1.0\n",
            UnsupportedWheel,
        ),
        ("Metadata-Version: 2.2\nName: other\nVersion: 1.0\n", InvalidWheelFilename),
        ("Metadata-Version: 2.2\nName: package\nVersion: 2.0\n", InvalidWheelFilename),
        ("Name: package\nVersion: 1.0\n", UnsupportedWheel),
        ("Metadata-Version: invalid\nName: package\nVersion: 1.0\n", UnsupportedWheel),
        (
            "Metadata-Version: 2.2\nMetadata-Version: 2.2\n"
            "Name: package\nVersion: 1.0\n",
            UnsupportedWheel,
        ),
        (
            b"Metadata-Version: 2.2\nName: package\nVersion: 1.0\nSummary: bad\xff\n",
            UnsupportedWheel,
        ),
    ],
)
def test_invalid_built_metadata(
    tmp_path: Path, metadata: str | bytes, error: type[InstallationError]
) -> None:
    contents = metadata.encode("utf-8") if isinstance(metadata, str) else metadata
    path = make_wheel("package", "1.0", metadata=contents).save_to_dir(tmp_path)
    with pytest.raises(error):
        verify_built_wheel(path)


@pytest.mark.parametrize("output", ["failure", "corrupt", "missing"])
def test_failed_wheel_metadata_cleans_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    req = install_req_from_line(f"package @ {tmp_path.as_uri()}")
    req.source_dir = str(tmp_path)
    req.pep517_backend = BuildBackendHookCaller(str(tmp_path), "backend")
    directories: list[Path] = []

    def build(
        name: str,
        backend: BuildBackendHookCaller,
        metadata_directory: str | None,
        wheel_directory: str,
    ) -> str | None:
        directory = Path(wheel_directory)
        directories.append(directory)
        if output == "failure":
            return None
        wheel = directory / "package-1.0-py3-none-any.whl"
        if output != "missing":
            wheel.write_bytes(b"invalid wheel")
        return str(wheel)

    monkeypatch.setattr(req_install, "build_wheel_pep517", build)
    with global_tempdir_manager():
        with pytest.raises(InstallationError):
            req.prepare_wheel_metadata(False)
        assert directories[0].exists()
        assert req.prebuilt_wheel_path is None
        assert req.metadata_directory is None
    assert not directories[0].exists()
