"""Turning nab's proof into pip's conflict report.

The shape that broke it is a distribution that declares one dependency
several times, each line gated by a different marker: a pin per Python
version, or the same package listed under every extra that pulls it in.
The renderer named whichever line came first, which is a line the resolve
never read, and pip drops a line whose marker does not hold, so the
message was left with nothing to say about the conflict.

The derivation is built from stand-ins here, because a proof is read
through ``cause``, ``terms``, ``cause_left`` and ``cause_right`` and
nothing else. Everything the renderer reads back is real: a real
provider holding real parsed metadata, a real fetch port, a real host
index and pip's own factory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from pip._vendor.nab_provider.metadata import WheelMetadata
from pip._vendor.nab_provider.provider import Provider as NabProvider
from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import Version

from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.nab.candidates import PipHostIndex
from pip._internal.resolution.nab.engine import _DerivationReader
from pip._internal.resolution.nab.errors import (
    FailureCause,
    causes_from_derivation,
    to_installation_error,
)
from pip._internal.resolution.nab.fetch_port import PipFetchPort
from pip._internal.resolution.nab.inputs import ResolveInputs, collect_inputs

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pip._internal.index.package_finder import PackageFinder
    from pip._internal.resolution.model.factory import Factory


ROOT_SENTINEL = object()

# paddleocr's shape: one numpy pin per Python, the first of which the
# running interpreter rules out.
PADDLEX = {
    ("paddlex", "3.0.0"): [
        'numpy==1.24.4; python_version < "3"',
        'numpy==1.26.4; python_version >= "3"',
    ]
}


@dataclass(frozen=True)
class _Cause:
    """Stands in for nab's ``IncompatibilityCause``, read by name only."""

    name: str


@dataclass(frozen=True)
class _Term:
    package: str
    constraint: Any = None
    positive: bool = True

    def is_positive(self) -> bool:
        return self.positive


@dataclass(frozen=True)
class _Clause:
    """Stands in for one node of nab's derivation tree."""

    cause: _Cause
    terms: tuple[_Term, ...] = ()
    cause_left: _Clause | None = None
    cause_right: _Clause | None = None


@dataclass(frozen=True)
class _AnyVersion:
    """A clause range that covers every version, as a widened one does."""

    def __contains__(self, version: object) -> bool:
        return True


def _inputs(*lines: str) -> ResolveInputs:
    return collect_inputs(
        [install_req_from_line(line) for line in lines], ignore_dependencies=False
    )


@pytest.fixture
def index(finder: PackageFinder, factory: Factory) -> PipHostIndex:
    return PipHostIndex(
        factory=factory,
        finder=finder,
        inputs=_inputs("numpy~=2.0"),
        upgrade_strategy="to-satisfy-only",
        make_install_req=install_req_from_line,
    )


@pytest.fixture
def port(index: PipHostIndex) -> PipFetchPort:
    return PipFetchPort(
        host=index, python_version=Version("3.11"), ignore_requires_python=False
    )


def _provider(
    port: PipFetchPort, entries: dict[tuple[str, str], list[str]]
) -> NabProvider:
    """A provider whose caches hold ``entries`` and nothing else.

    The error path never fetches: it reads back the metadata the resolve
    already parsed, so seeding the caches is seeding the whole input.
    """
    provider = NabProvider(port)
    for (name, raw_version), requires_dist in entries.items():
        version = Version(raw_version)
        provider.metadata_cache[(name, version)] = WheelMetadata(
            name=name,
            version=version,
            requires_dist=[Requirement(line) for line in requires_dist],
        )
        provider.deps_cache[(name, version)] = {}
    return provider


def _reader(
    port: PipFetchPort,
    index: PipHostIndex,
    entries: dict[tuple[str, str], list[str]],
    *requirements: str,
) -> _DerivationReader:
    inputs = _inputs(*requirements)
    return _DerivationReader(_provider(port, entries), port, inputs, index)


def test_requirement_text_names_the_line_whose_marker_holds(
    port: PipFetchPort, index: PipHostIndex
) -> None:
    reader = _reader(port, index, PADDLEX, "numpy~=2.0")

    text = reader.requirement_text("paddlex", "numpy", Version("3.0.0"))

    assert text == 'numpy==1.26.4; python_version >= "3"'


def test_requirement_text_names_the_line_of_the_node_extra(
    port: PipFetchPort, index: PipHostIndex
) -> None:
    entries = {
        ("jax", "0.4.17"): [
            'jaxlib==0.4.16; extra == "ci"',
            'jaxlib==0.4.17; extra == "cpu"',
        ]
    }
    reader = _reader(port, index, entries, "jax[cpu]")

    text = reader.requirement_text("jax[cpu]", "jaxlib", Version("0.4.17"))

    assert text == 'jaxlib==0.4.17; extra == "cpu"'


def test_requirement_text_does_not_credit_an_extra_line_to_the_base(
    port: PipFetchPort, index: PipHostIndex
) -> None:
    entries = {("jax", "0.4.17"): ['jaxlib==0.4.17; extra == "cpu"']}
    reader = _reader(port, index, entries, "jax")

    assert reader.requirement_text("jax", "jaxlib", Version("0.4.17")) is None


def test_requirement_text_does_not_credit_a_base_line_to_an_extra(
    port: PipFetchPort, index: PipHostIndex
) -> None:
    entries = {("jax", "0.4.17"): ["jaxlib==0.4.17"]}
    reader = _reader(port, index, entries, "jax[cpu]")

    assert reader.requirement_text("jax[cpu]", "jaxlib", Version("0.4.17")) is None


def test_requirement_text_answers_per_version(
    port: PipFetchPort, index: PipHostIndex
) -> None:
    entries = {
        ("paddlex", "2.0.0"): ["numpy<2"],
        ("paddlex", "3.0.0"): ["numpy>=2"],
    }
    reader = _reader(port, index, entries, "numpy~=2.0")

    assert reader.requirement_text("paddlex", "numpy", Version("2.0.0")) == "numpy<2"
    assert reader.requirement_text("paddlex", "numpy", Version("3.0.0")) == "numpy>=2"


def _conflict() -> _Clause:
    """paddlex wants a numpy that is left with no version."""
    dependency = _Clause(
        cause=_Cause("DEPENDENCY"),
        terms=(_Term("paddlex", _AnyVersion()), _Term("numpy", positive=False)),
    )
    no_versions = _Clause(cause=_Cause("NO_VERSIONS"), terms=(_Term("numpy"),))
    return _Clause(
        cause=_Cause("DERIVED"), cause_left=dependency, cause_right=no_versions
    )


def _causes_for(reader: _DerivationReader) -> Sequence[FailureCause]:
    return causes_from_derivation(
        _conflict(),
        root_sentinel=ROOT_SENTINEL,
        root_causes=reader.root_causes,
        requirement_text=reader.requirement_text,
        parent_versions=reader.parent_versions,
        requires_python=reader.requires_python,
        blockers=reader.blockers,
    )


def test_the_cause_carries_the_line_the_resolve_read(
    port: PipFetchPort, index: PipHostIndex
) -> None:
    reader = _reader(port, index, PADDLEX, "numpy~=2.0")

    causes = _causes_for(reader)

    assert [cause.requirement for cause in causes] == [
        'numpy==1.26.4; python_version >= "3"'
    ]
    assert causes[0].parent_version == Version("3.0.0")


def test_a_dependency_no_version_declares_falls_back_to_the_name(
    port: PipFetchPort, index: PipHostIndex
) -> None:
    reader = _reader(port, index, {("paddlex", "3.0.0"): []}, "numpy~=2.0")

    causes = _causes_for(reader)

    assert [cause.requirement for cause in causes] == ["numpy"]


def test_the_report_names_the_line_the_resolve_read(
    factory: Factory, port: PipFetchPort, index: PipHostIndex
) -> None:
    inputs = _inputs("numpy~=2.0")
    reader = _reader(port, index, PADDLEX, "numpy~=2.0")

    error = to_installation_error(
        _causes_for(reader),
        factory=factory,
        index=index,
        inputs=inputs,
        fallback="nab said no",
    )

    assert "numpy==1.26.4" in str(error)
    assert "1.24.4" not in str(error)


def test_a_marker_suppressed_cause_is_dropped_rather_than_asserted(
    factory: Factory, index: PipHostIndex
) -> None:
    """A line the environment removed is not a cause pip can state.

    This was a bare ``assert`` on the rendering path, so a proof that
    reached one exited with a traceback instead of an error message.
    """
    cause = FailureCause(
        requirement='numpy==1.24.4; python_version < "3"',
        parent_key="paddlex",
        parent_project_name=canonicalize_name("paddlex"),
        parent_version=Version("3.0.0"),
    )

    error = to_installation_error(
        [cause],
        factory=factory,
        index=index,
        inputs=_inputs("numpy~=2.0"),
        fallback="nab said no",
    )

    assert str(error) == "nab said no"
