from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path
from zipfile import ZipFile

import pytest

from tests.lib import (
    PipTestEnvironment,
    create_basic_sdist_for_package,
    create_basic_wheel_for_package,
)


def repack_sdist(archive: Path, extension: str, compression: str) -> Path:
    tar_contents = gzip.decompress(archive.read_bytes())
    destination = archive.with_name(archive.name.removesuffix(".tar.gz") + extension)
    if compression == "zip":
        with tarfile.open(fileobj=io.BytesIO(tar_contents)) as source:
            with ZipFile(destination, "w") as target:
                for member in source.getmembers():
                    if member.isfile():
                        contents = source.extractfile(member)
                        assert contents is not None
                        target.writestr(member.name, contents.read())
    elif compression == "bz2":
        bz2 = pytest.importorskip("bz2")
        destination.write_bytes(bz2.compress(tar_contents))
    elif compression in {"xz", "lzma"}:
        lzma = pytest.importorskip("lzma")
        archive_format = lzma.FORMAT_XZ if compression == "xz" else lzma.FORMAT_ALONE
        destination.write_bytes(lzma.compress(tar_contents, format=archive_format))
    elif compression == "tar":
        destination.write_bytes(tar_contents)
    else:
        assert compression == "gzip"
        destination.write_bytes(gzip.compress(tar_contents))
    return destination


@pytest.mark.parametrize(
    "extension, compression",
    [
        (".tar.gz", "gzip"),
        (".tgz", "gzip"),
        (".tar", "tar"),
        (".zip", "zip"),
        (".tar.bz2", "bz2"),
        (".tbz", "bz2"),
        (".tar.xz", "xz"),
        (".txz", "xz"),
        (".tlz", "lzma"),
        (".tar.lzma", "lzma"),
    ],
)
def test_legacy_archive_resolves_modern_metadata_without_backend(
    script: PipTestEnvironment, extension: str, compression: str
) -> None:
    for name in ("dependency", "accelerator"):
        create_basic_wheel_for_package(script, name, "1.0")
    archive = create_basic_sdist_for_package(
        script,
        "demo-package",
        "1.0",
        extra_files={
            "PKG-INFO": (
                "Metadata-Version: 2.2\nName: demo-package\nVersion: 1.0\n"
                "Requires-Dist: dependency==1.0\nProvides-Extra: speed\n"
                'Requires-Dist: accelerator==1.0; extra == "speed"\n'
            ),
            "pyproject.toml": (
                '[build-system]\nrequires = []\nbuild-backend = "backend"\n'
                'backend-path = ["."]\n'
            ),
            "backend.py": "raise RuntimeError('legacy backend executed')\n",
        },
    )
    archive = repack_sdist(archive, extension, compression)

    result = script.pip(
        "download",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--dest",
        "downloaded",
        f"demo-package[speed] @ {archive.as_uri()}",
    )

    for filename in (
        archive.name,
        "dependency-1.0-py2.py3-none-any.whl",
        "accelerator-1.0-py2.py3-none-any.whl",
    ):
        result.did_create(Path("scratch/downloaded") / filename)


@pytest.mark.parametrize(
    "filename, metadata, flags",
    [
        (
            "main.zip",
            "Metadata-Version: 2.2\nName: demo-package\nVersion: 1.0\n",
            ["--no-deps", "--ignore-requires-python"],
        ),
        (
            "other-1.0.zip",
            "Metadata-Version: 2.2\nName: demo-package\nVersion: 1.0\n",
            [],
        ),
        (
            "demo-package-2.0.zip",
            "Metadata-Version: 2.2\nName: demo-package\nVersion: 1.0\n",
            [],
        ),
        (
            "demo-package-1.0.zip",
            "Metadata-Version: 2.1\nName: demo-package\nVersion: 1.0\n",
            ["--no-deps", "--ignore-requires-python"],
        ),
        (
            "demo-package-1.0.zip",
            None,
            ["--no-deps", "--ignore-requires-python"],
        ),
        (
            "demo-package-1.0.zip",
            "Metadata-Version: 2.2\nName: demo-package\nVersion: 1.0\n"
            "Dynamic: Requires-Dist\n",
            [],
        ),
    ],
)
def test_legacy_archive_without_trustworthy_metadata_uses_backend(
    script: PipTestEnvironment, filename: str, metadata: str | None, flags: list[str]
) -> None:
    files = {
        "pyproject.toml": (
            '[build-system]\nrequires = []\nbuild-backend = "backend"\n'
            'backend-path = ["."]\n'
        ),
        "backend.py": "raise RuntimeError('legacy backend executed')\n",
    }
    if metadata is not None:
        files["PKG-INFO"] = metadata
    archive = create_basic_sdist_for_package(
        script, "demo-package", "1.0", extra_files=files
    )
    archive = repack_sdist(archive, ".zip", "zip").rename(
        script.scratch_path / filename
    )

    result = script.pip("download", "--no-index", *flags, archive, expect_error=True)

    assert "legacy backend executed" in result.stderr


def test_legacy_archive_static_metadata_keeps_hash_checks(
    script: PipTestEnvironment,
) -> None:
    archive = create_basic_sdist_for_package(
        script,
        "demo-package",
        "1.0",
        extra_files={
            "PKG-INFO": "Metadata-Version: 2.2\nName: demo-package\nVersion: 1.0\n",
            "pyproject.toml": (
                '[build-system]\nrequires = []\nbuild-backend = "backend"\n'
                'backend-path = ["."]\n'
            ),
            "backend.py": "raise RuntimeError('legacy backend executed')\n",
        },
    )
    archive = repack_sdist(archive, ".zip", "zip")

    result = script.pip(
        "download",
        "--no-index",
        "--dest",
        "downloaded",
        f"{archive.as_uri()}#sha256={'0' * 64}",
        expect_error=True,
    )

    assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr
    assert "legacy backend executed" not in result.stderr
    result.did_not_create(Path("scratch/downloaded") / archive.name)


def test_legacy_archive_root_metadata_does_not_describe_subdirectory(
    script: PipTestEnvironment,
) -> None:
    archive = create_basic_sdist_for_package(
        script,
        "demo-package",
        "1.0",
        extra_files={
            "PKG-INFO": "Metadata-Version: 2.2\nName: demo-package\nVersion: 1.0\n",
            "child/pyproject.toml": (
                '[build-system]\nrequires = []\nbuild-backend = "backend"\n'
                'backend-path = ["."]\n'
            ),
            "child/backend.py": "raise RuntimeError('subdirectory backend executed')\n",
        },
    )
    archive = repack_sdist(archive, ".zip", "zip")

    result = script.pip(
        "download",
        "--no-index",
        f"{archive.as_uri()}#subdirectory=child",
        expect_error=True,
    )

    assert "subdirectory backend executed" in result.stderr
