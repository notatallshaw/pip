from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pip._internal.build_env import InprocessBuildEnvironmentInstaller
from pip._internal.cache import WheelCache
from pip._internal.models.target_python import TargetPython
from pip._internal.operations.build.build_tracker import get_build_tracker
from pip._internal.utils.temp_dir import global_tempdir_manager

from tests.lib import make_test_finder, make_wheel


@pytest.mark.parametrize("policy", ["default", "only-binary", "no-binary"])
def test_build_dependency_finder_selects_host_wheel(
    tmp_path: Path, policy: str
) -> None:
    host_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    wheels = {}
    for tag in (host_tag, "cp39"):
        wheels[tag] = make_wheel(
            "backend",
            "1.0",
            wheel_metadata_updates={"Tag": f"{tag}-none-any"},
            extra_files={"backend.py": ""},
        ).save_to(tmp_path / f"backend-1.0-{tag}-none-any.whl")
    outer = make_test_finder(
        find_links=[str(tmp_path)],
        target_python=TargetPython(py_version_info=(3, 9)),
    )
    if policy == "only-binary":
        outer.format_control.only_binary.add("backend")
    elif policy == "no-binary":
        outer.format_control.no_binary.add("backend")

    cached = outer.find_best_candidate("backend")
    if policy == "no-binary":
        assert cached.best_candidate is None
    else:
        assert cached.best_candidate is not None
        assert cached.best_candidate.link.file_path == wheels["cp39"]

    with global_tempdir_manager(), get_build_tracker() as tracker:
        installer = InprocessBuildEnvironmentInstaller(
            finder=outer, build_tracker=tracker, wheel_cache=WheelCache(None)
        )
        selected = installer._finder.find_best_candidate("backend").best_candidate
        if policy == "no-binary":
            assert selected is None
        else:
            assert selected is not None
            assert selected.link.file_path == wheels[host_tag]

    # Creating the host finder leaves the outer target's cached selection intact.
    assert outer.find_best_candidate("backend") is cached
