import json

import pytest

from tests.lib import PipTestEnvironment
from tests.lib.wheel import make_wheel


@pytest.mark.parametrize("with_extra", [False, True])
@pytest.mark.parametrize("wrong_hash", [False, True])
def test_index_self_url_keeps_source_mode_and_checks_hashes(
    script: PipTestEnvironment, with_extra: bool, wrong_hash: bool
) -> None:
    path = script.scratch_path / "pkg-2.0-py2.py3-none-any.whl"
    marker = ' ; extra == "speed"' if with_extra else ""
    fragment = "egg=pkg" + ("&sha256=" + "0" * 64 if wrong_hash else "")
    wheel = make_wheel(
        name="pkg",
        version="2.0",
        metadata=(
            "Metadata-Version: 2.1\nName: pkg\nVersion: 2.0\nProvides-Extra: speed\n"
            f"Requires-Dist: pkg @ {path.as_uri()}#{fragment}{marker}\n"
        ),
    )
    wheel.save_to(path)
    requirement = "pkg[speed]==2.0" if with_extra else "pkg==2.0"
    report = script.scratch_path / "report.json"
    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--report",
        report,
        requirement,
        expect_error=wrong_hash,
    )
    if wrong_hash:
        assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr
        script.assert_not_installed("pkg")
        return

    script.assert_installed(pkg="2.0")
    script.pip("check")
    installed = json.loads(report.read_text())["install"]
    assert len(installed) == 1
    assert installed[0]["is_direct"] is False
