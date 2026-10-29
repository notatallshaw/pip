from pathlib import Path

from tests.lib import PipTestEnvironment
from tests.lib.wheel import make_wheel


def test_download_hash_mismatch_keeps_original_local_wheel(
    script: PipTestEnvironment,
) -> None:
    wheel = Path(make_wheel("demo", "1.0").save_to_dir(script.scratch_path))
    contents = wheel.read_bytes()

    result = script.pip(
        "download",
        "--no-index",
        "--no-deps",
        f"{wheel.as_uri()}#sha256={'0' * 64}",
        expect_error=True,
    )

    assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr
    assert wheel.read_bytes() == contents
