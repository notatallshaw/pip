"""Check nab alias registration without installing external dependencies."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PROBE = """
import importlib
from pathlib import Path
import sys
import pip._vendor as vendor

source, external, first = sys.argv[1:]
assert Path(vendor.__file__).is_relative_to(source)
initializer = Path(vendor.__file__).read_text().replace(
    "DEBUNDLED = False", "DEBUNDLED = True"
)
exec(compile(initializer, vendor.__file__, "exec"), vendor.__dict__)
importlib.import_module("pip._vendor.nab_resolver." + first)

for package, modules in {
    "nab_resolver": (
        "errors", "priority", "predicate_ranges", "proofs", "ranges",
        "resolver", "result", "root", "types", "values",
    ),
    "nab_provider": ("callbacks", "candidate_stream"),
}.items():
    for name in modules:
        bundled = importlib.import_module("pip._vendor." + package + "." + name)
        canonical = importlib.import_module(package + "." + name)
        assert bundled is canonical, "split module: " + name
        assert Path(canonical.__file__).is_relative_to(external)

from pip._vendor.nab_provider.callbacks import CallbackProvider, CandidateQuery
from pip._vendor.nab_resolver.predicate_ranges import PredicateRange
from pip._vendor.nab_resolver.resolver import Resolver
from pip._vendor.nab_resolver.errors import ResolutionError
from nab_resolver.errors import ResolutionError as CanonicalError
from pip._internal.resolution.nab.host import PipHost
assert ResolutionError is CanonicalError

class Host:
    def identify(self, value):
        return value[0]
    def is_satisfied_by(self, requirement, candidate):
        return candidate[1] in requirement[1]
    def find_matches(self, package, requirements, incompatibilities):
        return CandidateQuery(
            candidate for candidate in {10: [(10, 1)], 20: [(20, 2)]}[package]
            if candidate not in incompatibilities.get(package, ()) and all(
                self.is_satisfied_by(item, candidate)
                for item in requirements[package]
            )
        )
    def get_dependencies(self, candidate):
        return [(20, frozenset({2}))] if candidate[0] == 10 else []
    def exact_candidate(self, requirement):
        return None
    def defer_absence(self, package):
        return False
    def preference(self, package, requirements):
        return package

provider = CallbackProvider(Host(), [(10, frozenset({1}))])
resolver = Resolver(
    provider, range_type=PredicateRange,
    availability_generation=provider.availability_generation,
)
solution = resolver.solve(provider.root_requirements())
assert solution.pins == {10: (10, 1), 20: (20, 2)}
assert solution.edges == ((10, 20),)
assert solution.roots == (10,)
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
        [sys.executable, "-S", "-c", _PROBE, str(source), str(tmp_path), first],
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(source), str(tmp_path)))},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
