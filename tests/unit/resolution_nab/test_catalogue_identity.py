"""Retain concrete files, version labels and acquisition modes."""

from pathlib import Path

from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.nab.candidates import as_base_candidate
from pip._internal.resolution.nab.factory import Factory
from pip._internal.resolution.nab.host import PipHost

from tests.lib.wheel import make_wheel


def test_equal_versions_keep_distinct_files_and_original_labels(
    factory: Factory, tmp_path: Path
) -> None:
    choices = []
    for directory, version in (("first", "1"), ("second", "1"), ("third", "1.0")):
        folder = tmp_path / directory
        folder.mkdir()
        wheel = Path(make_wheel("probe", version).save_to_dir(folder))
        (requirement,) = factory.make_requirements_from_spec(wheel.as_uri(), None)
        candidate, _ = requirement.get_candidate_lookup()
        assert candidate is not None
        choices.append(candidate)
    assert len(set(choices)) == 3
    assert [str(choice.version) for choice in choices] == ["1", "1", "1.0"]
    for choice in choices:
        assert as_base_candidate(choice) is choice


def test_prepared_source_keys_keep_direct_and_index_provenance(
    factory: Factory,
) -> None:
    collected = factory.collect_root_requirements(
        [install_req_from_line("simplewheel")]
    )
    host = PipHost(
        factory,
        collected,
        ignore_dependencies=False,
        upgrade_strategy="to-satisfy-only",
    )
    ordinary = next(
        iter(
            host.find_matches(
                "simplewheel", {"simplewheel": collected.requirements}, {}
            )
        )
    )
    assert ordinary.source_link is not None
    (requirement,) = factory.make_requirements_from_spec(
        f"simplewheel @ {ordinary.source_link.url}", None
    )
    direct, _ = requirement.get_candidate_lookup()
    assert direct is not None
    indexed_requirement = ordinary.get_install_requirement()
    direct_requirement = direct.get_install_requirement()
    assert indexed_requirement is not None
    assert direct_requirement is not None
    assert not indexed_requirement.is_direct
    assert direct_requirement.is_direct
    assert len({ordinary, direct}) == 2

    base = as_base_candidate(direct)
    assert base is not None
    extras = factory._make_extras_candidate(base, frozenset({"feature"}))
    assert extras.base is base
    assert extras.source_link == direct.source_link
