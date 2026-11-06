"""Pip prerelease checks use only complete reachable cached metadata."""

from pip._vendor.nab_provider.version_candidates import VersionCandidateData
from pip._vendor.packaging.ranges import VersionRange
from pip._vendor.packaging.version import Version

from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.nab.catalogue import CatalogueProvider
from pip._internal.resolution.nab.factory import Factory
from pip._internal.resolution.nab.provider import PipProvider
from pip._internal.resolution.nab.yank_candidates import Candidate, YankCandidates


def catalogue(factory: Factory, provider: PipProvider) -> CatalogueProvider:
    """Create the real catalogue over a single unprepared root."""
    collected = factory.collect_root_requirements([install_req_from_line("probe")])
    return CatalogueProvider(factory, provider, collected)


def test_catalogue_selected_closure_refuses_unread_or_missing_choices(
    factory: Factory, provider: PipProvider
) -> None:
    cached = catalogue(factory, provider)
    root, child = Version("1"), Version("2")
    assert cached.complete_selection("probe", root) is None

    cached.dependencies["probe", root] = (), {"child": VersionRange.full()}
    assert cached.complete_selection("probe", root) is None

    cached.solution_decisions = {"child": child, "unrelated": Version("9")}
    cached.dependencies["child", child] = (), {"probe": VersionRange.full()}
    assert cached.complete_selection("probe", root) == {"probe": root, "child": child}
    assert cached.complete_selection("unrelated", Version("9")) is None


def test_yank_prerelease_check_keeps_incomplete_metadata_unresolved(
    factory: Factory, provider: PipProvider
) -> None:
    cached = catalogue(factory, provider)
    cached.has_conditional_prereleases = True
    choices = YankCandidates(cached)
    root = Candidate("probe", Version("1a1"), withdrawn=False)
    child = Candidate("child", Version("2"), withdrawn=False)

    assert choices.metadata_is_admitted(root, {})
    assert choices.metadata_is_admitted(root, {"probe": root})

    choices.dependencies[root] = (), {"child": VersionRange.full()}
    assert choices.metadata_is_admitted(root, {"probe": root})
    assert choices.metadata_is_admitted(root, {"probe": root, "child": child})

    choices.dependencies[child] = (), {"probe": VersionRange.full()}
    choices.facts[root] = VersionCandidateData({"child": VersionRange.full()}, ())
    unrelated = Candidate("unrelated", Version("9"), withdrawn=False)
    assert choices.metadata_is_admitted(unrelated, {"probe": root, "child": child})
