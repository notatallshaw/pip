from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from io import BytesIO

import pytest

from pip._vendor.packaging.markers import (
    EvaluateContext,
    Marker,
    UndefinedComparison,
    UndefinedEnvironmentName,
)
from pip._vendor.packaging.requirements import InvalidRequirement

from pip._internal.metadata import BaseDistribution, MemoryWheel
from pip._internal.metadata.importlib import Distribution

from tests.lib.wheel import make_wheel


def distribution(
    requirements: Sequence[str], extras: Sequence[str] = ()
) -> BaseDistribution:
    """Read dependency headers through the real importlib wheel backend."""
    wheel = make_wheel(
        "probe",
        "1.0",
        metadata_updates={
            "Requires-Dist": list(requirements),
            "Provides-Extra": list(extras),
        },
    )
    return Distribution.from_wheel(
        MemoryWheel("probe-1.0-py3-none-any.whl", BytesIO(wheel.as_bytes())), "probe"
    )


@pytest.fixture
def marker_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> list[Mapping[str, str | AbstractSet[str]] | None]:
    """Record evaluation contexts while executing the real marker evaluator."""
    calls: list[Mapping[str, str | AbstractSet[str]] | None] = []
    original = Marker.evaluate

    def evaluate(
        self: Marker,
        environment: Mapping[str, str | AbstractSet[str]] | None = None,
        context: EvaluateContext = "metadata",
    ) -> bool:
        calls.append(environment)
        return original(self, environment, context)

    monkeypatch.setattr(Marker, "evaluate", evaluate)
    return calls


def test_validation_reuses_markers_only_within_one_call(
    marker_calls: list[Mapping[str, str | AbstractSet[str]] | None],
) -> None:
    dist = distribution(
        ['numpy>=1.17; extra == "dev"', 'packaging>=20; extra == "dev"'],
        ["doc", "dev"],
    )

    dist.validate_dependencies()
    assert marker_calls == [{"extra": "doc"}, {"extra": "dev"}]

    marker_calls.clear()
    dist.validate_dependencies()
    assert marker_calls == [{"extra": "doc"}, {"extra": "dev"}]

    # Selection still returns both requirements, including their markers.
    selected = list(dist.iter_dependencies(["dev"]))
    assert [r.name for r in selected] == ["numpy", "packaging"]
    assert all(r.marker is not None for r in selected)
    assert list(dist.iter_dependencies()) == []


@pytest.mark.parametrize(
    "requirements,extras,error,message",
    [
        (["dep=>1"], [], InvalidRequirement, "dep=>1"),
        (['dep; python_version => "3"'], [], InvalidRequirement, "python_version"),
        (['dep=>1; extra == "unused"'], ["doc"], InvalidRequirement, "dep=>1"),
        (['dep; os_name ~= "posix"'], [], UndefinedComparison, "Undefined"),
        (
            ['dep; extra == "unused" and os_name ~= "posix"'],
            ["doc"],
            UndefinedComparison,
            "Undefined",
        ),
        (['dep; "extra" == "gpu"'], [], UndefinedEnvironmentName, "gpu"),
        (
            ['dep; os_name ~= "posix"', "broken=>1"],
            [],
            UndefinedComparison,
            "Undefined",
        ),
        (
            ['dep; extra == "doc"', 'other; extra == "doc"', "broken=>1"],
            ["doc"],
            InvalidRequirement,
            "broken=>1",
        ),
        ([r"dep; os_name ~= '\x22\x27'"], [], UndefinedComparison, "Undefined"),
        (
            [r"dep; os_name == '\x22\x27'", "broken=>1"],
            [],
            InvalidRequirement,
            "broken=>1",
        ),
    ],
)
def test_validation_preserves_errors_and_their_order(
    requirements: list[str],
    extras: list[str],
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        distribution(requirements, extras).validate_dependencies()


@pytest.mark.parametrize(
    "requirements,extras",
    [
        (["dep>=1"], []),
        (['dep; extra == "unused"', 'other; extra == "unused"'], ["doc"]),
        (['dep; extra == ""', 'other; extra == ""'], []),
        (['dep; extra == "foo-bar"'], ["Foo_Bar", "foo-bar"]),
    ],
)
def test_validation_accepts_matching_and_unmatched_markers(
    requirements: list[str], extras: list[str]
) -> None:
    distribution(requirements, extras).validate_dependencies()


def test_unserializable_markers_are_evaluated_without_a_memo(
    marker_calls: list[Mapping[str, str | AbstractSet[str]] | None],
) -> None:
    dist = distribution(
        [r"dep; os_name == '\x22\x27'", r"other; os_name == '\x22\x27'"]
    )

    dist.validate_dependencies()

    assert marker_calls == [{"extra": ""}, {"extra": ""}]


def test_changed_metadata_is_validated_again() -> None:
    dist = distribution(['dep; extra == "doc"'], ["doc"])
    dist.validate_dependencies()
    dist.metadata["Requires-Dist"] = 'other; os_name ~= "posix"'

    with pytest.raises(UndefinedComparison):
        dist.validate_dependencies()
