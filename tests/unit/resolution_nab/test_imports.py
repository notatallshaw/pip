"""Check that install and build-environment entry points depend only on nab."""

import os
import subprocess
import sys
from pathlib import Path


def test_resolver_imports_without_resolvelib(tmp_path: Path) -> None:
    """Reject even attempted imports of the removed vendor package."""
    source = Path(__file__).parents[3] / "src"
    wheel = source.parent / "tests/data/packages/simplewheel-1.0-py2.py3-none-any.whl"
    program = """
import importlib.abc
import sys

class DenyResolvelib(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        prefix = "pip._vendor.resolvelib"
        if fullname == prefix or fullname.startswith(prefix + "."):
            raise AssertionError("resolvelib import attempted: " + fullname)

sys.meta_path.insert(0, DenyResolvelib())
from pip._internal.cli.main import main
from pip._internal.build_env.installer import InprocessBuildEnvironmentInstaller
from pip._internal.resolution.nab.resolver import Resolver

assert Resolver.__module__ == "pip._internal.resolution.nab.resolver"
assert main(["install", "--dry-run", "--ignore-installed", "--no-deps",
             "--no-index", "--disable-pip-version-check", sys.argv[1]]) == 0
assert not any(name.startswith("pip._vendor.resolvelib") for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(wheel)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(source)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
