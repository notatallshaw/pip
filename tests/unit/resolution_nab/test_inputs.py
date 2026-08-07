"""Splitting pip's root ireqs into resolver inputs."""

from __future__ import annotations

import pytest

from pip._vendor.packaging.utils import canonicalize_name

from pip._internal.exceptions import InstallationError, UnsupportedWheel
from pip._internal.models.link import Link
from pip._internal.req.constructors import install_req_from_line
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


def test_specifier_for_ands_requirements_and_constraints() -> None:
    inputs = collect_inputs(
        [_ireq("foo>=1"), _ireq("foo<3", constraint=True)],
        ignore_dependencies=False,
    )
    specifier = inputs.specifier_for(canonicalize_name("foo"))
    assert set(str(specifier).split(",")) == {">=1", "<3"}


def test_a_requirements_own_link_is_checked_before_anything_resolves() -> None:
    """pip refuses an unusable wheel at input time, not as a failed search.

    Left to the candidate builder it becomes an empty universe, and the
    error renderer then lists the index to write its message, which is one
    request pip never makes and a worse sentence.
    """
    checked = []

    def check(link: Link) -> None:
        checked.append(link.filename)
        raise UnsupportedWheel(f"{link.filename} is not a supported wheel")

    with pytest.raises(UnsupportedWheel):
        collect_inputs(
            [_ireq("/tmp/simple.dist-0.1-py1-none-invalid.whl")],
            ignore_dependencies=False,
            check_link=check,
        )
    assert checked == ["simple.dist-0.1-py1-none-invalid.whl"]


def test_a_constraints_link_is_left_to_the_candidate_builder() -> None:
    """pip swallows the same exception for a link a constraint names."""
    inputs = collect_inputs(
        [
            _ireq("simple.dist"),
            _ireq("/tmp/simple.dist-0.1-py1-none-invalid.whl", constraint=True),
        ],
        ignore_dependencies=False,
        check_link=_refuse_every_link,
    )
    assert [r.key for r in inputs.requirements] == [canonicalize_name("simple.dist")]


def _refuse_every_link(link: Link) -> None:
    raise UnsupportedWheel(f"{link.filename} is not a supported wheel")
