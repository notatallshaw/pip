"""Splitting pip's root ireqs into resolver inputs."""

from __future__ import annotations

import pathlib

import pytest

from pip._vendor.packaging.utils import NormalizedName, canonicalize_name

from pip._internal.exceptions import InstallationError, UnsupportedWheel
from pip._internal.models.link import Link
from pip._internal.req.constructors import install_req_from_line
from pip._internal.req.req_install import InstallRequirement
from pip._internal.resolution.nab.inputs import collect_inputs
from pip._internal.utils.deprecation import PipDeprecationWarning


def _ireq(line: str, *, constraint: bool = False, user_supplied: bool = True):  # type: ignore[no-untyped-def]
    return install_req_from_line(
        line, constraint=constraint, user_supplied=user_supplied
    )


def test_collect_inputs_records_command_line_order() -> None:
    inputs = collect_inputs(
        [_ireq("first"), _ireq("second"), _ireq("third")],
        ignore_dependencies=False,
    )
    assert inputs.user_requested == {"first": 0, "second": 1, "third": 2}


def test_collect_inputs_splits_a_specifier_with_extras() -> None:
    inputs = collect_inputs([_ireq("foo[bar]>=1")], ignore_dependencies=False)
    assert [requirement.key for requirement in inputs.requirements] == [
        "foo",
        "foo[bar]",
    ]


def test_collect_inputs_splits_a_link_with_extras(tmp_path: pathlib.Path) -> None:
    source = tmp_path / "LocalExtras"
    source.mkdir()
    (source / "setup.py").write_text("")

    def name_link(ireq: InstallRequirement) -> NormalizedName:
        return canonicalize_name("localextras")

    inputs = collect_inputs(
        [_ireq(f"{source}[bar]")],
        ignore_dependencies=False,
        name_link=name_link,
    )
    assert [requirement.key for requirement in inputs.requirements] == [
        "localextras",
        "localextras[bar]",
    ]
    assert all(
        requirement.link is not None for requirement in inputs.requirements
    ), "a link requirement keeps its link on both halves"


def test_collect_inputs_drops_extras_from_the_base_half_text() -> None:
    inputs = collect_inputs([_ireq("foo[bar]>=1")], ignore_dependencies=False)
    assert [requirement.text for requirement in inputs.requirements] == [
        "foo>=1",
        "foo[bar]>=1",
    ]


def test_collect_inputs_keeps_the_requirement_as_the_user_wrote_it() -> None:
    inputs = collect_inputs([_ireq("requirements.txt")], ignore_dependencies=False)
    assert [requirement.text for requirement in inputs.requirements] == [
        "requirements.txt"
    ]


def test_roots_for_returns_every_requirement_on_one_node() -> None:
    inputs = collect_inputs(
        [_ireq("pkg[ext1]>1"), _ireq("pkg==1.0")], ignore_dependencies=False
    )
    assert [root.text for root in inputs.roots_for("pkg")] == ["pkg>1", "pkg==1.0"]
    assert [root.text for root in inputs.roots_for("pkg[ext1]")] == ["pkg[ext1]>1"]


def test_collect_inputs_puts_extras_requirements_last() -> None:
    inputs = collect_inputs(
        [_ireq("foo[bar]"), _ireq("baz")], ignore_dependencies=False
    )
    assert [requirement.key for requirement in inputs.requirements] == [
        "baz",
        "foo[bar]",
    ]


def test_collect_inputs_ands_constraint_lines() -> None:
    inputs = collect_inputs(
        [
            _ireq("foo>=1", constraint=True),
            _ireq("foo<2", constraint=True),
        ],
        ignore_dependencies=False,
    )
    constraint = inputs.constraints[canonicalize_name("foo")]
    assert set(str(constraint.specifier).split(",")) == {">=1", "<2"}


def test_collect_inputs_drops_a_requirement_whose_markers_do_not_apply() -> None:
    inputs = collect_inputs(
        [_ireq('foo; python_version < "2.0"')], ignore_dependencies=False
    )
    assert inputs.requirements == []
    assert inputs.user_requested == {}


def test_collect_inputs_rejects_an_invalid_constraint() -> None:
    with (
        pytest.warns(PipDeprecationWarning),
        pytest.raises(InstallationError, match="Constraints cannot have extras"),
    ):
        collect_inputs([_ireq("foo[bar]", constraint=True)], ignore_dependencies=False)


def test_pinned_packages_sees_a_pin_from_a_constraint_line() -> None:
    inputs = collect_inputs(
        [_ireq("foo"), _ireq("foo==1.0", constraint=True)],
        ignore_dependencies=False,
    )
    assert inputs.pinned_packages() == frozenset({canonicalize_name("foo")})


def test_pinned_packages_ignores_a_range() -> None:
    inputs = collect_inputs([_ireq("foo>=1.0")], ignore_dependencies=False)
    assert inputs.pinned_packages() == frozenset()


def test_collect_inputs_refuses_a_link_the_platform_cannot_install() -> None:
    """A wheel this platform cannot use is a hard error, as it is in pip.

    ``Factory._make_requirements_from_install_req`` raises ``UnsupportedWheel``
    for a requirement's own link before anything tries to resolve it, so the
    message reaches stderr rather than becoming an empty candidate universe
    that the error renderer then has to list the index to explain.
    """
    checked: list[Link] = []

    def check_link(link: Link) -> None:
        checked.append(link)
        raise UnsupportedWheel(f"{link.filename} is not a supported wheel")

    with pytest.raises(UnsupportedWheel, match="not a supported wheel"):
        collect_inputs(
            [_ireq("pkg @ https://example.com/pkg-1.0-py1-none-invalid.whl")],
            ignore_dependencies=False,
            check_link=check_link,
        )
    assert [link.filename for link in checked] == ["pkg-1.0-py1-none-invalid.whl"]


def test_collect_inputs_does_not_check_a_requirement_with_no_link() -> None:
    def check_link(link: Link) -> None:
        raise AssertionError("checked a requirement that names no link")

    inputs = collect_inputs(
        [_ireq("pkg>=1")], ignore_dependencies=False, check_link=check_link
    )
    assert [requirement.key for requirement in inputs.requirements] == ["pkg"]


def test_specifier_for_ands_requirements_and_constraints() -> None:
    inputs = collect_inputs(
        [_ireq("foo>=1"), _ireq("foo<3", constraint=True)],
        ignore_dependencies=False,
    )
    specifier = inputs.specifier_for(canonicalize_name("foo"))
    assert set(str(specifier).split(",")) == {">=1", "<3"}
