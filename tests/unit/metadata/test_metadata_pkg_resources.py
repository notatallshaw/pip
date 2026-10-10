import email.message
import itertools
from typing import cast
from unittest import mock

import pytest

from pip._vendor.packaging.markers import UndefinedComparison, UndefinedEnvironmentName
from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
from pip._vendor.packaging.specifiers import SpecifierSet
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import parse as parse_version

from pip._internal.exceptions import InstallationError, UnsupportedWheel
from pip._internal.metadata.pkg_resources import (
    Distribution,
    Environment,
    InMemoryMetadata,
)

pkg_resources = pytest.importorskip("pip._vendor.pkg_resources")

_VALIDATION_METADATA = b"Name: probe\nVersion: 1.0\n"


def _dist_is_local(dist: mock.Mock) -> bool:
    return dist.kind != "global" and dist.kind != "user"


def _dist_in_usersite(dist: mock.Mock) -> bool:
    return dist.kind == "user"


@pytest.fixture(autouse=True)
def patch_distribution_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Distribution, "local", property(_dist_is_local))
    monkeypatch.setattr(Distribution, "in_usersite", property(_dist_in_usersite))


class _MockWorkingSet(list[mock.Mock]):
    def require(self, name: str) -> None:
        pass


workingset = _MockWorkingSet(
    (
        mock.Mock(test_name="global", project_name="global"),
        mock.Mock(test_name="editable", project_name="editable"),
        mock.Mock(test_name="normal", project_name="normal"),
        mock.Mock(test_name="user", project_name="user"),
    )
)

workingset_stdlib = _MockWorkingSet(
    (
        mock.Mock(test_name="normal", project_name="argparse"),
        mock.Mock(test_name="normal", project_name="wsgiref"),
    )
)


@pytest.mark.parametrize(
    "ws, req_name",
    [
        *itertools.product(
            [workingset],
            (d.project_name for d in workingset),
        ),
        *itertools.product(
            [workingset_stdlib],
            (d.project_name for d in workingset_stdlib),
        ),
    ],
)
def test_get_distribution(ws: _MockWorkingSet, req_name: str) -> None:
    """Ensure get_distribution() finds all kinds of distributions."""
    dist = Environment(ws).get_distribution(req_name)
    assert dist is not None
    assert cast(Distribution, dist)._dist.project_name == req_name


def test_get_distribution_nonexist() -> None:
    dist = Environment(workingset).get_distribution("non-exist")
    assert dist is None


def test_wheel_metadata_works() -> None:
    name = "simple"
    version = "0.1.0"
    require_a = "a==1.0"
    require_b = 'b==1.1; extra == "also_b"'
    requires = [require_a, require_b, 'c==1.2; extra == "also_c"']
    extras = ["also_b", "also_c"]
    requires_python = ">=3"

    metadata = email.message.Message()
    metadata["Name"] = name
    metadata["Version"] = version
    for require in requires:
        metadata["Requires-Dist"] = require
    for extra in extras:
        metadata["Provides-Extra"] = extra
    metadata["Requires-Python"] = requires_python

    dist = Distribution(
        pkg_resources.DistInfoDistribution(
            location="<in-memory>",
            metadata=InMemoryMetadata({"METADATA": metadata.as_bytes()}, "<in-memory>"),
            project_name=name,
        ),
    )

    assert name == dist.canonical_name == dist.raw_name
    assert parse_version(version) == dist.version
    assert {canonicalize_name(e) for e in extras} == set(dist.iter_provided_extras())
    assert [require_a] == [str(r) for r in dist.iter_dependencies()]
    assert [Requirement(require_a), Requirement(require_b)] == [
        Requirement(str(r)) for r in dist.iter_dependencies(["also_b"])
    ]
    assert metadata.as_string() == dist.metadata.as_string()
    assert SpecifierSet(requires_python) == dist.requires_python


def test_wheel_metadata_throws_on_bad_unicode() -> None:
    metadata = InMemoryMetadata({"METADATA": b"\xff"}, "<in-memory>")

    with pytest.raises(UnsupportedWheel) as e:
        metadata.get_metadata("METADATA")
    assert "METADATA" in str(e.value)


def test_iter_entry_points_throws_on_invalid_entry_point() -> None:
    dist = Distribution(
        pkg_resources.DistInfoDistribution(
            location="<in-memory>",
            metadata=InMemoryMetadata(
                {"entry_points.txt": b"[console_scripts]\nhello = hello:\n"},
                "<in-memory>",
            ),
            project_name="simple",
        ),
    )

    with pytest.raises(InstallationError) as e:
        list(dist.iter_entry_points())
    assert "hello = hello:" in str(e.value)


@pytest.mark.parametrize(
    "files,error,message",
    [
        ({"requires.txt": b"[unused:not_a_marker]\ndep\n"}, None, ""),
        ({"requires.txt": b"dep>=1 # comment\n"}, None, ""),
        ({"depends.txt": b"broken=>1\n"}, InvalidRequirement, "broken=>1"),
        (
            {
                "PKG-INFO": b"Name: probe\nVersion: 1.0\nRequires-Dist: valid\n",
                "requires.txt": b"broken=>1\n",
            },
            InvalidRequirement,
            "broken=>1",
        ),
        (
            {
                "METADATA": _VALIDATION_METADATA
                + b'Requires-Dist: dep; os_name ~= "posix"\n'
            },
            UndefinedComparison,
            "Undefined",
        ),
        (
            {
                "METADATA": (
                    _VALIDATION_METADATA
                    + b'Provides-Extra: unused\nRequires-Dist: dep; extra == "unused" '
                    b'and os_name ~= "posix"\n'
                )
            },
            UndefinedComparison,
            "Undefined",
        ),
        (
            {
                "METADATA": _VALIDATION_METADATA
                + b'Requires-Dist: dep; "extra" == "gpu"\n'
            },
            UndefinedEnvironmentName,
            "gpu",
        ),
        (
            {
                "METADATA": (
                    _VALIDATION_METADATA + b'Requires-Dist: dep; os_name ~= "posix"\n'
                    b"Requires-Dist: broken=>1\n"
                )
            },
            InvalidRequirement,
            "broken=>1",
        ),
    ],
)
def test_dependency_validation_retains_legacy_backend_rules(
    files: dict[str, bytes], error: type[Exception] | None, message: str
) -> None:
    metadata = {"PKG-INFO": b"Name: probe\nVersion: 1.0\n", **files}
    implementation = (
        pkg_resources.DistInfoDistribution
        if "METADATA" in metadata
        else pkg_resources.Distribution
    )
    dist = Distribution(
        implementation(
            project_name="probe",
            version="1.0",
            location="<in-memory>",
            metadata=InMemoryMetadata(metadata, "<in-memory>"),
        )
    )

    if error is None:
        dist.validate_dependencies()
    else:
        with pytest.raises(error, match=message):
            dist.validate_dependencies()
