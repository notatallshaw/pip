from pip._vendor.packaging.ranges import VersionRange
from pip._vendor.packaging.specifiers import SpecifierSet
from pip._vendor.packaging.version import Version

from pip._internal.resolution.nab.catalogue import Candidate, PipCatalogue
from pip._internal.resolution.nab.factory import Factory


def catalogue(factory: Factory) -> PipCatalogue:
    return PipCatalogue(
        factory,
        factory.collect_root_requirements([]),
        ignore_dependencies=False,
        upgrade_strategy="to-satisfy-only",
    )


def test_order_results_remain_caller_owned(factory: Factory) -> None:
    owner = catalogue(factory)
    newest, older = (
        Candidate("probe", Version(version), withdrawn=False) for version in ("2", "1")
    )
    owner.candidates["probe"] = newest, older
    allowed = VersionRange.full()

    result = owner.ordered("probe", allowed)
    assert result == [newest, older]
    result.clear()
    assert owner.ordered("probe", allowed) == [newest, older]


def test_explicit_subset_does_not_replace_complete_order(factory: Factory) -> None:
    owner = catalogue(factory)
    newest, older = (
        Candidate("probe", Version(version), withdrawn=False) for version in ("2", "1")
    )
    owner.candidates["probe"] = newest, older
    allowed = VersionRange.full()

    assert owner.ordered("probe", allowed) == [newest, older]
    assert owner.ordered("probe", allowed, choices=(older,)) == [older]
    assert owner.ordered("probe", allowed) == [newest, older]


def test_replaced_snapshot_is_ordered_afresh(factory: Factory) -> None:
    owner = catalogue(factory)
    newest, older = (
        Candidate("probe", Version(version), withdrawn=False) for version in ("2", "1")
    )
    owner.candidates["probe"] = newest, older
    allowed = VersionRange.full()
    assert owner.ordered("probe", allowed) == [newest, older]

    owner.candidates["probe"] = (older,)
    assert owner.ordered("probe", allowed) == [older]


def test_original_version_labels_and_bounds_remain_distinct(factory: Factory) -> None:
    owner = catalogue(factory)
    short, long = (
        Candidate("probe", Version(version), withdrawn=False)
        for version in ("1", "1.0")
    )
    owner.candidates["probe"] = short, long

    assert owner.ordered("probe", SpecifierSet("===1").to_range()) == [short]
    assert owner.ordered("probe", SpecifierSet("===1.0").to_range()) == [long]
    assert owner.ordered("probe", SpecifierSet(">=2").to_range()) == []
    assert owner.ordered("probe", VersionRange.full()) == [short, long]
