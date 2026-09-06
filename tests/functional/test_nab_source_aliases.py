import pytest

from tests.lib import PipTestEnvironment
from tests.lib.wheel import make_wheel


@pytest.mark.parametrize("with_extra", [False, True])
def test_index_candidate_can_require_its_own_equivalent_url(
    script: PipTestEnvironment, with_extra: bool
) -> None:
    path = script.scratch_path / "pkg-2.0-py2.py3-none-any.whl"
    marker = ' ; extra == "speed"' if with_extra else ""
    wheel = make_wheel(
        name="pkg",
        version="2.0",
        metadata=(
            "Metadata-Version: 2.1\nName: pkg\nVersion: 2.0\nProvides-Extra: speed\n"
            f"Requires-Dist: pkg @ {path.as_uri()}#egg=pkg{marker}\n"
        ),
    )
    wheel.save_to(path)
    requirement = "pkg[speed]==2.0" if with_extra else "pkg==2.0"
    script.pip(
        "install", "--no-index", "--find-links", script.scratch_path, requirement
    )
    script.assert_installed(pkg="2.0")
