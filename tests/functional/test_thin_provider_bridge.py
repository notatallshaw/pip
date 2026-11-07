"""Exercise native installation actions through the callback prototype."""

import hashlib
import json
from pathlib import Path

from tests.lib import PipTestEnvironment
from tests.lib.wheel import make_wheel


def test_same_version_index_replacement_updates_installed_metadata(
    script: PipTestEnvironment,
) -> None:
    initial = make_wheel(
        "dep",
        "1",
        metadata_updates={"Requires-Dist": ["missing==1"]},
        extra_files={"dep/__init__.py": "marker = 'installed'\n"},
    ).save_to_dir(script.scratch_path)
    script.pip("install", "--no-index", "--no-deps", initial)
    index = script.scratch_path / "index"
    index.mkdir()
    replacement = Path(
        make_wheel(
            "dep",
            "1",
            extra_files={"dep/__init__.py": "marker = 'index'\n"},
        ).save_to_dir(index)
    )
    report = script.scratch_path / "report.json"

    script.pip(
        "install", "--no-index", "--find-links", index, "--report", report, "dep"
    )

    selected = json.loads(report.read_text())["install"]
    assert len(selected) == 1
    assert selected[0]["download_info"]["url"] == replacement.as_uri()
    assert selected[0]["download_info"]["archive_info"]["hashes"]["sha256"] == (
        hashlib.sha256(replacement.read_bytes()).hexdigest()
    )
    installed = script.run(
        "python",
        "-c",
        "import dep; from importlib.metadata import distribution; "
        "assert dep.marker == 'index'; assert not distribution('dep').requires",
    )
    assert installed.returncode == 0
    script.pip("check")
