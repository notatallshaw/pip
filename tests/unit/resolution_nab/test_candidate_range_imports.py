"""Source ranges work without the provider's candidate and target policies."""

import subprocess
import sys

_PROBE = """
import sys

for name in (
    "dataclasses",
    "pip._vendor.nab_markersets",
    "pip._vendor.nab_provider.provider",
    "pip._vendor.nab_provider.fetch_port",
    "pip._vendor.nab_provider.target",
    "pip._vendor.nab_provider.marker_holds",
    "pip._vendor.nab_provider.resolver_inputs",
):
    sys.modules[name] = None

from pip._vendor.packaging.version import Version
from pip._internal.resolution.nab.ranges import CandidateKey, CandidateRange
from pip._vendor.nab_resolver.resolver import BaseProvider, Resolver

installed = CandidateKey(Version("1"), "installed")
direct = CandidateKey(Version("1"), "direct")
child = CandidateKey(Version("2"), "index")
catalog = {"demo": (installed, direct), "child": (child,)}

class SourceProvider(BaseProvider):
    def choose_version(self, package, allowed):
        return next((key for key in catalog[package] if key in allowed), None)

    def has_satisfying_version(self, package, allowed):
        return self.choose_version(package, allowed) is not None

    def get_dependencies(self, package, key):
        if package == "demo" and key == direct:
            return {"child": CandidateRange.singleton(child)}
        return {}

    def prioritize(self, package, allowed, conflicts, culprits=None):
        return package

    def widen_decision(self, package, key):
        return None

resolver = Resolver(
    SourceProvider(), range_type=CandidateRange,
    root_version=CandidateKey(Version("0"), "root"),
)
solution = resolver.solve({"demo": CandidateRange.singleton(direct)})
assert solution.pins == {"demo": direct, "child": child}
assert solution.edges == (("demo", "child"),)
"""


def test_solve_with_only_source_ranges() -> None:
    """Select distinct metadata for a direct source at an installed version."""
    subprocess.run([sys.executable, "-c", _PROBE], check=True)
