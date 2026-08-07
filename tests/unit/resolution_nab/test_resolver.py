"""The resolve that must not list the index.

pip's previous resolver answered "already satisfied" with zero index
requests, because the installed distribution is element zero of a lazy
candidate sequence (``resolution/model/found_candidates.py``). Under nab the
same fact has to arrive through the preference seam, and no test that has a
working index can tell the difference: the listing is a request the answer
does not need, not a wrong answer. That is why this defect survived v1.

So the finder here fails the moment anything lists a package, and the whole
resolver is driven against it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pip._vendor.packaging.utils import canonicalize_name

from pip._internal.metadata import BaseDistribution, get_metadata_distribution
from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.nab.resolver import Resolver

if TYPE_CHECKING:
    from typing import NoReturn

    from pip._internal.index.package_finder import PackageFinder
    from pip._internal.operations.prepare import RequirementPreparer


def _installed_dist(name: str, version: str) -> BaseDistribution:
    text = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n"
    return get_metadata_distribution(
        text.encode(), f"{name}-{version}.dist-info", str(canonicalize_name(name))
    )


def _refuse_to_list(project_name: str) -> NoReturn:
    raise AssertionError(f"listed {project_name}")


def _resolver(
    finder: PackageFinder, preparer: RequirementPreparer, **kwargs: object
) -> Resolver:
    options: dict[str, object] = {
        "use_user_site": False,
        "ignore_dependencies": False,
        "only_dependencies": False,
        "ignore_installed": False,
        "ignore_requires_python": False,
        "force_reinstall": False,
        "upgrade_strategy": "to-satisfy-only",
    }
    options.update(kwargs)
    return Resolver(
        preparer=preparer,
        finder=finder,
        wheel_cache=None,
        make_install_req=install_req_from_line,
        **options,  # type: ignore[arg-type]
    )


def _pins(resolver: Resolver) -> list[tuple[str, str]]:
    """What the engine decided, which an empty ``RequirementSet`` hides."""
    assert resolver._solution is not None
    return sorted((pin.key, str(pin.version)) for pin in resolver._solution.pins)


def test_an_already_satisfied_requirement_never_lists_the_index(
    finder: PackageFinder,
    preparer: RequirementPreparer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(finder, "find_all_candidates", _refuse_to_list)
    resolver = _resolver(finder, preparer)
    monkeypatch.setitem(
        resolver.factory._installed_dists,
        canonicalize_name("pkg"),
        _installed_dist("pkg", "1.0"),
    )

    req_set = resolver.resolve(
        [install_req_from_line("pkg", user_supplied=True)],
        check_supported_wheels=True,
    )

    # Nothing to install is what "already satisfied" means: an installed
    # candidate has no install requirement.
    assert list(req_set.requirements) == []
    assert _pins(resolver) == [("pkg", "1.0")]


def test_a_bounded_requirement_the_installed_version_meets_lists_nothing(
    finder: PackageFinder,
    preparer: RequirementPreparer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bounds are tested against the seeded version, not the listing."""
    monkeypatch.setattr(finder, "find_all_candidates", _refuse_to_list)
    resolver = _resolver(finder, preparer)
    monkeypatch.setitem(
        resolver.factory._installed_dists,
        canonicalize_name("pkg"),
        _installed_dist("pkg", "1.5"),
    )

    resolver.resolve(
        [install_req_from_line("pkg>=1,<2", user_supplied=True)],
        check_supported_wheels=True,
    )

    assert _pins(resolver) == [("pkg", "1.5")]


def test_bounds_the_installed_version_misses_still_reach_the_index(
    finder: PackageFinder,
    preparer: RequirementPreparer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seed is a prefix of the universe, never the universe.

    The moment the installed version is out of range the listing is what
    answers, so a seed that does not fit must not be able to hide the index.
    """
    monkeypatch.setattr(finder, "find_all_candidates", _refuse_to_list)
    resolver = _resolver(finder, preparer)
    monkeypatch.setitem(
        resolver.factory._installed_dists,
        canonicalize_name("pkg"),
        _installed_dist("pkg", "1.5"),
    )

    with pytest.raises(AssertionError, match="listed pkg"):
        resolver.resolve(
            [install_req_from_line("pkg>=2", user_supplied=True)],
            check_supported_wheels=True,
        )
