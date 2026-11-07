"""Retain the concrete file and acquisition mode used by pip."""

from pip._vendor.packaging.version import Version

from pip._internal.models.candidate import InstallationCandidate
from pip._internal.models.link import Link
from pip._internal.resolution.nab.candidates import as_base_candidate
from pip._internal.resolution.nab.catalogue import Candidate, PipCatalogue
from pip._internal.resolution.nab.factory import Factory


def test_equal_versions_keep_distinct_files_and_original_labels() -> None:
    choices = [
        Candidate(
            "probe",
            Version(version),
            withdrawn=False,
            artifact=InstallationCandidate("probe", version, Link(url)),
        )
        for version, url in (
            ("1", "https://index.invalid/probe-1-py3-none-any.whl"),
            ("1", "https://index.invalid/probe-1.tar.gz"),
            ("1.0", "https://index.invalid/probe-1.0-py3-none-any.whl"),
        )
    ]

    assert len(set(choices)) == 3
    assert [choice.label for choice in choices] == ["1", "1", "1.0"]
    for choice in choices:
        assert PipCatalogue.base_candidate(choice) == choice
        assert PipCatalogue.base_candidate(choice).artifact is choice.artifact


def test_prepared_source_keys_keep_direct_and_index_provenance(
    factory: Factory,
) -> None:
    artifact = factory.index_candidates("simplewheel")[0]
    ordinary = factory.prepare_index_candidate("simplewheel", artifact)
    assert ordinary is not None
    assert ordinary.source_link is not None
    (explicit,) = factory.make_requirements_from_spec(
        f"simplewheel @ {ordinary.source_link.url}", comes_from=None
    )
    direct, _ = explicit.get_candidate_lookup()
    assert direct is not None

    assert factory.candidate_source_key(ordinary) == (
        ordinary.source_link,
        False,
        False,
    )
    assert factory.candidate_source_key(direct) == (ordinary.source_link, False, True)

    base = as_base_candidate(direct)
    assert base is not None
    extras = factory.make_extras_candidate(base, frozenset({"feature"}))
    assert factory.candidate_source_key(extras) == factory.candidate_source_key(direct)
