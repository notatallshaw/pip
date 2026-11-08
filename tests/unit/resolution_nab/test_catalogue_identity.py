"""Retain the concrete file and acquisition mode used by pip."""

import os
import subprocess
import sys

from pip._vendor.packaging.version import Version

from pip._internal.models.candidate import InstallationCandidate
from pip._internal.models.link import Link
from pip._internal.resolution.nab.candidates import as_base_candidate
from pip._internal.resolution.nab.catalogue import Candidate, PipCatalogue
from pip._internal.resolution.nab.factory import Factory


def test_catalogue_identity_rehashes_after_cross_seed_pickle() -> None:
    setup = """import pickle, sys
from pip._vendor.packaging.version import Version
from pip._internal.models.candidate import InstallationCandidate
from pip._internal.models.link import Link
from pip._internal.resolution.nab.catalogue import Candidate
artifact = InstallationCandidate('probe', '1.0', Link('https://index.invalid/probe-1.0-py3-none-any.whl'))
candidate = Candidate('probe', Version('1.0'), withdrawn=False, artifact=artifact)
"""
    payload = subprocess.check_output(
        [
            sys.executable,
            "-c",
            setup + "sys.stdout.buffer.write(pickle.dumps({candidate: 'metadata'}))",
        ],
        env={**os.environ, "PYTHONHASHSEED": "0"},
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            setup
            + "assert pickle.loads(sys.stdin.buffer.read())[candidate] == 'metadata'",
        ],
        input=payload,
        env={**os.environ, "PYTHONHASHSEED": "1"},
        check=True,
    )


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


def test_source_modes_remain_distinct_in_candidate_sets() -> None:
    link = Link("file:///source/probe-1-py3-none-any.whl")
    modes = [(False, False), (True, False), (False, True)]
    choices = [
        Candidate(
            "probe", Version("1"), withdrawn=False, source_key=(link, editable, direct)
        )
        for editable, direct in modes
    ]
    equivalent = [
        Candidate(
            "probe",
            Version("1"),
            withdrawn=False,
            source_key=(Link(link.url), editable, direct),
        )
        for editable, direct in modes
    ]

    assert choices == equivalent
    assert len(set(choices)) == 3
    assert set(choices) == set(equivalent)
