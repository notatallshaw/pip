import pytest

from pip._vendor.packaging.utils import NormalizedName, canonicalize_name

from pip._internal.resolution.resolvelib.base import format_name, split_name
from pip._internal.resolution.resolvelib.candidates import REQUIRES_PYTHON_IDENTIFIER


@pytest.mark.parametrize(
    "identifier, expected",
    [
        ("pkg", ("pkg", frozenset())),
        ("pkg[extra]", ("pkg", frozenset(["extra"]))),
        ("pkg[a,b]", ("pkg", frozenset(["a", "b"]))),
        (REQUIRES_PYTHON_IDENTIFIER, (REQUIRES_PYTHON_IDENTIFIER, frozenset())),
    ],
)
def test_split_name(identifier: str, expected: tuple[str, frozenset[str]]) -> None:
    assert split_name(identifier) == expected


@pytest.mark.parametrize(
    "project, extras",
    [
        ("pkg", frozenset()),
        ("pkg", frozenset(["extra"])),
        ("pkg", frozenset(["b", "a"])),
        ("oslo-concurrency", frozenset(["test"])),
    ],
)
def test_split_name_inverts_format_name(project: str, extras: frozenset[str]) -> None:
    name = canonicalize_name(project)
    normalized = frozenset(canonicalize_name(e) for e in extras)
    assert split_name(format_name(name, normalized)) == (name, normalized)


def test_format_name_of_requires_python_identifier_round_trips() -> None:
    """The Requires-Python identifier has no bracket, so it splits to itself."""
    identifier: NormalizedName = REQUIRES_PYTHON_IDENTIFIER
    assert format_name(identifier, frozenset()) == identifier
    assert split_name(identifier) == (identifier, frozenset())
