"""The shared RequirementSet builder.

``resolution/_reqset.py`` is the code both resolver variants end in, so it
is tested against the protocol rather than against either variant's
candidate class.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import Version

from pip._internal.models.link import Link
from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution._reqset import build_requirement_set

if TYPE_CHECKING:
    from pip._vendor.packaging.utils import NormalizedName

    from pip._internal.metadata import BaseDistribution
    from pip._internal.req.req_install import InstallRequirement


@dataclass
class FakeCandidate:
    name: str
    version: Version
    ireq: InstallRequirement | None
    is_editable: bool = False
    source_link: Link | None = None

    @property
    def project_name(self) -> NormalizedName:
        return canonicalize_name(self.name.partition("[")[0])

    def get_install_requirement(self) -> InstallRequirement | None:
        return self.ireq


@dataclass
class FakeInstalled:
    version: Version
    editable: bool = False


def _candidate(line: str, name: str | None = None, **kwargs: object) -> FakeCandidate:
    ireq = install_req_from_line(line)
    assert ireq.req is not None
    return FakeCandidate(
        name=name or str(ireq.req.name),
        version=Version(str(ireq.req.specifier).lstrip("=")),
        ireq=ireq,
        **kwargs,  # type: ignore[arg-type]
    )


def _nothing_installed(candidate: FakeCandidate) -> BaseDistribution | None:
    return None


def test_nothing_installed_means_no_reinstall() -> None:
    candidate = _candidate("simple==1.0")
    req_set = build_requirement_set(
        [candidate],
        check_supported_wheels=True,
        get_dist_to_uninstall=_nothing_installed,
        force_reinstall=False,
        only_dependencies=False,
        user_requested=(),
    )
    assert list(req_set.requirements) == ["simple"]
    assert req_set.requirements["simple"].should_reinstall is False


def test_a_different_installed_version_forces_a_reinstall() -> None:
    candidate = _candidate("simple==2.0")
    req_set = build_requirement_set(
        [candidate],
        check_supported_wheels=True,
        get_dist_to_uninstall=lambda c: FakeInstalled(Version("1.0")),  # type: ignore[arg-type,return-value]
        force_reinstall=False,
        only_dependencies=False,
        user_requested=(),
    )
    assert req_set.requirements["simple"].should_reinstall is True


def test_the_same_installed_version_is_skipped_entirely() -> None:
    candidate = _candidate("simple==1.0")
    req_set = build_requirement_set(
        [candidate],
        check_supported_wheels=True,
        get_dist_to_uninstall=lambda c: FakeInstalled(Version("1.0")),  # type: ignore[arg-type,return-value]
        force_reinstall=False,
        only_dependencies=False,
        user_requested=(),
    )
    assert list(req_set.requirements) == []


def test_force_reinstall_wins_over_an_identical_version() -> None:
    candidate = _candidate("simple==1.0")
    req_set = build_requirement_set(
        [candidate],
        check_supported_wheels=True,
        get_dist_to_uninstall=lambda c: FakeInstalled(Version("1.0")),  # type: ignore[arg-type,return-value]
        force_reinstall=True,
        only_dependencies=False,
        user_requested=(),
    )
    assert req_set.requirements["simple"].should_reinstall is True


def test_extras_collapse_back_onto_the_base_requirement() -> None:
    base = _candidate("simple==1.0")
    extras = FakeCandidate(name="simple[extra]", version=Version("1.0"), ireq=None)
    req_set = build_requirement_set(
        [extras, base],
        check_supported_wheels=True,
        get_dist_to_uninstall=_nothing_installed,
        force_reinstall=False,
        only_dependencies=False,
        user_requested=(),
    )
    assert list(req_set.requirements) == ["simple"]
    assert req_set.requirements["simple"].extras == {"extra"}


def test_only_dependencies_drops_what_the_user_asked_for() -> None:
    req_set = build_requirement_set(
        [_candidate("simple==1.0"), _candidate("other==1.0")],
        check_supported_wheels=True,
        get_dist_to_uninstall=_nothing_installed,
        force_reinstall=False,
        only_dependencies=True,
        user_requested=["simple[extra]"],
    )
    assert list(req_set.requirements) == ["other"]


def test_a_yanked_source_link_is_warned_about(caplog) -> None:  # type: ignore[no-untyped-def]
    candidate = _candidate(
        "simple==1.0",
        source_link=Link("https://example.com/simple-1.0.tar.gz", yanked_reason="oops"),
    )
    build_requirement_set(
        [candidate],
        check_supported_wheels=True,
        get_dist_to_uninstall=_nothing_installed,
        force_reinstall=False,
        only_dependencies=False,
        user_requested=(),
    )
    assert "yanked version" in caplog.text
    assert "oops" in caplog.text


def test_check_supported_wheels_is_carried_through() -> None:
    req_set = build_requirement_set(
        [],
        check_supported_wheels=False,
        get_dist_to_uninstall=_nothing_installed,
        force_reinstall=False,
        only_dependencies=False,
        user_requested=(),
    )
    assert req_set.check_supported_wheels is False
