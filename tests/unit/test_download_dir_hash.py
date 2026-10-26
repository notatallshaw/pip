import os
from pathlib import Path

import pytest

from pip._internal.exceptions import HashMismatch
from pip._internal.models.link import Link
from pip._internal.operations.prepare import _check_download_dir
from pip._internal.utils.hashes import Hashes


@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink"])
def test_bad_hash_preserves_local_input(tmp_path: Path, alias: str) -> None:
    source = tmp_path / "demo.whl"
    contents = b"original input bytes"
    source.write_bytes(contents)
    destination = source
    if alias != "same":
        download_dir = tmp_path / "downloaded"
        download_dir.mkdir()
        destination = download_dir / source.name
        try:
            if alias == "symlink":
                destination.symlink_to(source)
            else:
                os.link(source, destination)
        except OSError as error:
            pytest.skip(f"{alias} unavailable: {error}")

    with pytest.raises(HashMismatch):
        _check_download_dir(
            Link(source.as_uri()),
            str(destination.parent),
            Hashes({"sha256": ["0" * 64]}),
        )

    assert source.read_bytes() == contents
    assert destination.read_bytes() == contents


def test_bad_hash_still_discards_remote_download_cache(tmp_path: Path) -> None:
    cached = tmp_path / "demo.whl"
    cached.write_bytes(b"bad cached bytes")

    result = _check_download_dir(
        Link("https://example.invalid/demo.whl"),
        str(tmp_path),
        Hashes({"sha256": ["0" * 64]}),
    )

    assert result is None
    assert not cached.exists()
