from pip._vendor.nab_resolver.version_spans import cached_dependency_span
from pip._vendor.packaging.ranges import VersionRange
from pip._vendor.packaging.version import Version


def test_cached_span_stops_at_different_or_unread_metadata() -> None:
    versions = list(map(Version, ("1", "2", "3", "4")))
    dependencies = {
        ("app", versions[0]): ((), {"dep": VersionRange.singleton(Version("1"))}),
        ("app", versions[1]): ((), {}),
        ("app", versions[2]): ((), {}),
    }
    lower, upper = cached_dependency_span(
        versions[1],
        versions,
        lambda version: (
            record[1]
            if (record := dependencies.get(("app", version))) is not None
            else None
        ),
    )
    span = VersionRange.from_bounds(
        lower, upper, include_lower=False, include_upper=False
    )
    assert versions[1] in span
    assert versions[2] in span
    assert Version("2.5") in span

    assert versions[0] not in span
    assert versions[3] not in span


def test_missing_selected_metadata_does_not_absorb_unknown_neighbors() -> None:
    versions = list(map(Version, ("1", "2", "3")))
    lower, upper = cached_dependency_span(versions[1], versions, lambda _version: None)
    span = VersionRange.from_bounds(
        lower, upper, include_lower=False, include_upper=False
    )
    assert versions[1] in span
    assert versions[0] not in span
    assert versions[2] not in span


def test_prerelease_is_a_real_boundary_in_the_catalogue() -> None:
    versions = list(map(Version, ("1", "2a1", "2", "3")))
    lower, upper = cached_dependency_span(versions[2], versions, lambda _version: None)
    span = VersionRange.from_bounds(
        lower, upper, include_lower=False, include_upper=False
    )
    assert versions[2] in span
    assert versions[1] not in span
    assert versions[3] not in span
