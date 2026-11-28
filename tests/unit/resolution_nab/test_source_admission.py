"""Only active original declarations authorize a direct source."""

import pytest

from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.nab.catalogue import PipCatalogue
from pip._internal.resolution.nab.factory import Factory

from tests.lib import TestData


@pytest.mark.parametrize("rooted", [False, True])
def test_known_source_needs_an_active_declaration(
    factory: Factory, data: TestData, rooted: bool
) -> None:
    wheel = data.packages / "simplewheel-1.0-py2.py3-none-any.whl"
    source = f"simplewheel @ {wheel.as_uri()}"
    (requirement,) = factory.make_requirements_from_spec(source, comes_from=None)
    native, _ = requirement.get_candidate_lookup()
    assert native is not None
    # Discovery alone must not grant permission to select the source.
    factory.seed_source(native)

    collected = factory.collect_root_requirements(
        [install_req_from_line(source)]
        if rooted
        else [install_req_from_line("simplewheel")]
    )
    catalogue = PipCatalogue(
        factory,
        collected,
        ignore_dependencies=False,
        upgrade_strategy="to-satisfy-only",
    )
    choice = catalogue.source_candidate("simplewheel", native)
    assert catalogue.read_candidate(choice) is not None

    assert catalogue.selection_is_admitted({"simplewheel": choice}) is rooted
