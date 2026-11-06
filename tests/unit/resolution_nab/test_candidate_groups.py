"""Only candidate groups admitted by fixed inputs require proxy resolution."""

import pytest

from pip._vendor.packaging.ranges import VersionRange
from pip._vendor.packaging.version import Version

from pip._internal.index.package_finder import PackageFinder
from pip._internal.models.candidate import InstallationCandidate
from pip._internal.models.link import Link
from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.nab.catalogue import (
    CatalogueProvider,
    _TryYankedResolution,
)
from pip._internal.resolution.nab.factory import Factory
from pip._internal.resolution.nab.provider import PipProvider


@pytest.mark.parametrize("kind", ["yank", "text"])
@pytest.mark.parametrize(
    "requirements,identifier,constraints",
    [
        (["probe>=2"], "probe", []),
        (["probe", "probe>=2"], "probe", []),
        (["probe[opt]>=2"], "probe[opt]", []),
        (["probe[opt]>=2"], "probe", []),
        (["probe[opt]", "probe>=2"], "probe[opt]", []),
        (["probe[a]>=2", "probe[b]"], "probe[b]", []),
        (["probe"], "probe", ["probe>=2"]),
        (["probe[opt]"], "probe[opt]", ["probe>=2"]),
    ],
)
def test_excluded_groups_do_not_change_the_provider(
    factory: Factory,
    provider: PipProvider,
    finder: PackageFinder,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    requirements: list[str],
    identifier: str,
    constraints: list[str],
) -> None:
    artifacts = [
        InstallationCandidate(
            "probe",
            "1",
            Link(
                "https://index.invalid/probe-1-py3-none-any.whl",
                yanked_reason="withdrawn" if kind == "yank" else None,
            ),
        ),
        InstallationCandidate(
            "probe", "2", Link("https://index.invalid/probe-2-py3-none-any.whl")
        ),
    ]
    if kind == "text":
        artifacts.append(
            InstallationCandidate(
                "probe", "1.0", Link("https://index.invalid/probe-1.0-py3-none-any.whl")
            )
        )
    monkeypatch.setattr(finder, "find_all_candidates", lambda _name: artifacts)
    inputs = [install_req_from_line(item) for item in requirements]
    inputs.extend(install_req_from_line(item, constraint=True) for item in constraints)
    collected = factory.collect_root_requirements(inputs)
    catalogue = CatalogueProvider(factory, provider, collected)

    choices = catalogue.catalogue(identifier)

    assert choices.candidates
    assert identifier not in catalogue.token_catalogues
    assert not catalogue.has_yanked_candidates


@pytest.mark.parametrize("kind", ["yank", "text"])
def test_temporary_solver_bounds_cannot_hide_an_input_eligible_group(
    factory: Factory,
    provider: PipProvider,
    finder: PackageFinder,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    candidate = InstallationCandidate(
        "probe",
        "1",
        Link(
            "https://index.invalid/probe-1-py3-none-any.whl",
            yanked_reason="withdrawn" if kind == "yank" else None,
        ),
    )
    artifacts = [candidate]
    if kind == "text":
        artifacts.append(
            InstallationCandidate(
                "probe", "1.0", Link("https://index.invalid/probe-1.0-py3-none-any.whl")
            )
        )
    monkeypatch.setattr(finder, "find_all_candidates", lambda _name: artifacts)
    collected = factory.collect_root_requirements([install_req_from_line("probe")])
    catalogue = CatalogueProvider(factory, provider, collected)
    catalogue.receive_partial_solution_hint(
        {"probe": VersionRange.singleton(Version("2"))}, {}
    )

    with pytest.raises(_TryYankedResolution):
        catalogue.catalogue("probe")
    assert "probe" in catalogue.token_catalogues
    assert catalogue.has_yanked_candidates == (kind == "yank")
