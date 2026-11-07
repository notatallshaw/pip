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

from pip._internal.resolution.nab.catalogue import Candidate
from pip._vendor.packaging.version import Version
from nab_provider.version_candidates import VersionCandidate

assert isinstance(Candidate("probe", Version("1"), withdrawn=False), VersionCandidate)

for name in (
    "errors", "priority",
    "proofs", "ranges", "resolver", "result", "root", "types", "values",
):
    bundled = importlib.import_module("pip._vendor.nab_resolver." + name)
    canonical = importlib.import_module("nab_resolver." + name)
    assert bundled is canonical, "split nab module: " + name
    assert Path(canonical.__file__).is_relative_to(external)

for name in (
    "preference", "preference_provider", "prepared_resolution",
    "value", "version_candidates",
):
    bundled = importlib.import_module("pip._vendor.nab_provider." + name)
    canonical = importlib.import_module("nab_provider." + name)
    assert bundled is canonical, "split provider module: " + name
    assert Path(canonical.__file__).is_relative_to(external)

from pip._vendor.nab_resolver.root import ROOT
from nab_resolver.root import ROOT as canonical_root

assert ROOT is canonical_root

from pip._vendor.nab_provider.prepared_resolution import PreparedResolution
from pip._vendor.nab_provider.version_candidates import (
    PreparedVersionCatalogue, VersionCandidateData,
)
from pip._vendor.packaging.ranges import VersionRange

class Catalogue(PreparedVersionCatalogue):
    range_type = VersionRange

    @staticmethod
    def pin_project(pin):
        return pin.name

    @staticmethod
    def base_name(package):
        return package

    def read_choices(self, package):
        version = Version({"app": "1", "dep": "2"}[package])
        return (VersionCandidate(package, version, withdrawn=False),)

    def ordered(self, package, allowed):
        return self.order_candidates(package, allowed, self.choices(package))

    def read_candidate(self, candidate):
        dependencies = (
            {"dep": VersionRange.singleton(Version("2"))}
            if candidate.package == "app" else {}
        )
        return VersionCandidateData(dependencies, ())

catalogue = Catalogue(unavailable_reason="unavailable")
session = PreparedResolution.from_catalogue(
    catalogue, {"app": VersionRange.singleton(Version("1"))}, (), {},
)
solution = session.solve()
assert {name: str(item.version) for name, item in solution.candidates.items()} == {
    "app": "1", "dep": "2",
}
assert solution.edges == (("app", "dep"),)
assert solution.roots == ("app",)

"""


@pytest.mark.parametrize("first", ["result", "resolver", "errors"])
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
