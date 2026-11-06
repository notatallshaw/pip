"""Check nab alias registration without installing external dependencies."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PROBE = """
import importlib
from pathlib import Path
import sys

source, external, first = sys.argv[1:]
sys.path[:0] = [source, external]
import pip._vendor as vendor

initializer = Path(vendor.__file__).read_text()
assert initializer.count("DEBUNDLED = False") == 1
initializer = initializer.replace("DEBUNDLED = False", "DEBUNDLED = True")
exec(compile(initializer, vendor.__file__, "exec"), vendor.__dict__)

importlib.import_module("pip._vendor.nab_resolver." + first)

from pip._vendor.nab_resolver.result import IncompleteSelectionError
from nab_resolver.result import walk_selected

try:
    list(walk_selected(("missing",), {}, lambda _package, _candidate: ()))
except IncompleteSelectionError as error:
    assert error.package == "missing"
else:
    raise AssertionError("incomplete external selection was accepted")

from pip._internal.resolution.nab.ranges import CandidateKey
from pip._vendor.packaging.version import Version
from nab_resolver.values import FrozenValue

assert isinstance(CandidateKey(Version("1"), "index"), FrozenValue)

for name in (
    "candidate_provider", "diagnostics", "errors", "keyed_ranges", "priority",
    "proofs", "ranges", "resolver", "result", "root", "testing", "types", "values",
    "version_spans",
):
    bundled = importlib.import_module("pip._vendor.nab_resolver." + name)
    canonical = importlib.import_module("nab_resolver." + name)
    assert bundled is canonical, "split nab module: " + name
    assert Path(canonical.__file__).is_relative_to(external)

for name in (
    "preference", "preference_provider", "value", "version_candidates",
):
    bundled = importlib.import_module("pip._vendor.nab_provider." + name)
    canonical = importlib.import_module("nab_provider." + name)
    assert bundled is canonical, "split provider module: " + name
    assert Path(canonical.__file__).is_relative_to(external)

from pip._vendor.nab_resolver.root import ROOT
from nab_resolver.root import ROOT as canonical_root

assert ROOT is canonical_root

from pip._vendor.nab_resolver.candidate_provider import (
    CandidateProvider, CandidateRequirement, PreparedCandidate,
)
from pip._vendor.nab_resolver.ranges import Range
from pip._vendor.nab_resolver.resolver import Resolver

class Host:
    def iter_candidates(self, package, allowed, requirements):
        version = {"app": 1, "dep": 2}[package]
        if version in allowed:
            yield PreparedCandidate(version, package)

    def get_dependencies(self, candidate):
        if candidate.origin == "app":
            yield CandidateRequirement("dep", Range.singleton(2), "app dependency")

    def priority(self, package, requirements):
        return package

provider = CandidateProvider(
    Host(), [CandidateRequirement("app", Range.singleton(1), "root")],
)
solution = Resolver(provider, range_type=Range).solve(provider.root_requirements())
assert solution.pins == {"app": 1, "dep": 2}
assert provider.validate_solution(solution)
"""


@pytest.mark.parametrize("first", ["candidate_provider", "resolver", "testing"])
def test_debundled_nab_public_modules_share_identity(
    tmp_path: Path, first: str
) -> None:
    """Keep external exceptions, value types and solver roots coherent."""
    source = Path(__file__).parents[3] / "src"
    for package in ("nab_resolver", "nab_provider", "packaging"):
        target = tmp_path / package
        shutil.copytree(
            source / "pip/_vendor" / package,
            target,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        # External distributions import each other by their canonical names.
        for module in target.rglob("*.py"):
            module.write_text(module.read_text().replace("pip._vendor.", ""))

    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", _PROBE, str(source), str(tmp_path), first],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
