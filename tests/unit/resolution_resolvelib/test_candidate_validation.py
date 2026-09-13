from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from io import BytesIO
from pathlib import Path

import pytest

from pip._vendor.packaging.markers import EvaluateContext, Marker, UndefinedComparison
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import Version

from pip._internal.exceptions import MetadataInconsistent, MetadataInvalid
from pip._internal.metadata import BaseDistribution, MemoryWheel
from pip._internal.metadata.importlib import Distribution
from pip._internal.models.link import Link
from pip._internal.req.constructors import install_req_from_line
from pip._internal.req.req_install import InstallRequirement
from pip._internal.resolution.resolvelib.candidates import LinkCandidate
from pip._internal.resolution.resolvelib.factory import Factory

from tests.lib.wheel import make_wheel


def test_candidate_preparation_uses_scoped_dependency_validation(
    factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = make_wheel(
        "probe",
        "1.0",
        metadata_updates={
            "Requires-Dist": ['dep; extra == "doc"', 'other; extra == "doc"'],
            "Provides-Extra": ["doc"],
        },
    )
    dist = Distribution.from_wheel(
        MemoryWheel("probe-1.0-py3-none-any.whl", BytesIO(wheel.as_bytes())), "probe"
    )
    link = Link("https://index.invalid/probe-1.0-py3-none-any.whl")

    def prepare(
        requirement: InstallRequirement, *, parallel_builds: bool
    ) -> BaseDistribution:
        assert requirement.link == link
        return dist

    calls: list[Mapping[str, str | AbstractSet[str]] | None] = []
    original = Marker.evaluate

    def evaluate(
        self: Marker,
        environment: Mapping[str, str | AbstractSet[str]] | None = None,
        context: EvaluateContext = "metadata",
    ) -> bool:
        calls.append(environment)
        return original(self, environment, context)

    monkeypatch.setattr(factory.preparer, "prepare_linked_requirement", prepare)
    monkeypatch.setattr(Marker, "evaluate", evaluate)

    candidate = LinkCandidate(
        link,
        install_req_from_line(str(link)),
        factory,
        canonicalize_name("probe"),
        Version("1.0"),
    )

    assert candidate.dist is dist
    assert calls == [{"extra": "doc"}]


@pytest.mark.parametrize(
    "name,version,dependency,error",
    [
        ("other", "1.0", "broken=>1", MetadataInconsistent),
        ("probe", "2.0", "broken=>1", MetadataInconsistent),
        ("probe", "1.0", "broken=>1", MetadataInvalid),
        ("probe", "1.0", 'dep; os_name ~= "posix"', UndefinedComparison),
    ],
)
def test_candidate_preserves_consistency_checks_and_error_wrapping(
    factory: Factory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    version: str,
    dependency: str,
    error: type[Exception],
) -> None:
    wheel = make_wheel("probe", "1.0", metadata_updates={"Requires-Dist": [dependency]})
    dist = Distribution.from_wheel(
        MemoryWheel("probe-1.0-py3-none-any.whl", BytesIO(wheel.as_bytes())), "probe"
    )
    link = Link((tmp_path / "probe-1.0-py3-none-any.whl").as_uri())

    def prepare(
        requirement: InstallRequirement, *, parallel_builds: bool
    ) -> BaseDistribution:
        assert requirement.link == link
        return dist

    monkeypatch.setattr(factory.preparer, "prepare_linked_requirement", prepare)

    with pytest.raises(error):
        LinkCandidate(
            link,
            install_req_from_line(str(link)),
            factory,
            canonicalize_name(name),
            Version(version),
        )
