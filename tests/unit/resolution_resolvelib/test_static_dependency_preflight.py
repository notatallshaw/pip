import io
import tarfile
from pathlib import Path

import pytest

from pip._vendor.packaging.specifiers import SpecifierSet
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import Version

from pip._internal.distributions.sdist import SDIST_RESOLUTION_FIELDS
from pip._internal.metadata import get_metadata_distribution
from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.resolvelib.factory import (
    Factory,
    _has_pinned_version_conflict,
)

from tests.lib.wheel import make_wheel


@pytest.mark.parametrize(
    "left, right, conflict",
    [
        ("==1", "==2", True),
        ("==1", "==1.0", False),
        ("==1", "==1+local", False),
        ("==1+local", "==1", False),
        ("==1+local", "==1+other", True),
        ("==1+LOCAL", "==1.0.0+local", False),
        ("<2", "==2", True),
        (">2", "==2+local", True),
        ("<=1", "==1+local", False),
        ("!=2+local", "==2", False),
        ("!=2", "==2+local", True),
        ("===1", "==2", False),
        ("===1,==1", "==2", True),
        ("===1,>=2", "==1", True),
        ("===1", "==1+local", False),
        ("==1.*", "==2.*", False),
        ("==1.*", "==2", True),
        ("==1", "==2.*", True),
        ("==1+local", ">=1", False),
        ("==1", "!=1.*", True),
        (">=2", "<2", False),
        ("", "==1,==2", True),
        ("==1,==1+local", "==1.0", False),
        ("==1,!=1+local", "==1+local", True),
        ("==1,!=1+local", "==1", False),
        ("==1.0rc1", "==1.0rc1+local", False),
        ("==1.0.dev1", "==1.0.dev1+local", False),
        ("==1!1.0", "==1.0", True),
        ("~=1.4", "==2", True),
    ],
)
def test_pinned_version_conflicts(left: str, right: str, conflict: bool) -> None:
    assert (
        _has_pinned_version_conflict(SpecifierSet(left), SpecifierSet(right))
        == conflict
    )


@pytest.mark.parametrize(
    "root, constraint, dependency, extra, conflict",
    [
        ("dependency==1", False, "dependency==2", "", True),
        ("dependency==1", True, "dependency==2", "", True),
        ("dependency==1; python_version < '0'", False, "dependency==2", "", False),
        ("dependency==1; python_version < '0'", True, "dependency==2", "", False),
        ("dependency==1", False, "dependency==2; python_version < '0'", "", False),
        ("dependency==1", False, "dependency==2; extra == 'speed'", "", False),
        ("dependency==1", False, "dependency==2; extra == 'speed'", "speed", True),
        (
            "dependency==1",
            False,
            "dependency @ https://example.invalid/dependency-2-py3-none-any.whl",
            "",
            False,
        ),
    ],
)
def test_only_mandatory_applicable_dependency_conflicts(
    factory: Factory,
    root: str,
    constraint: bool,
    dependency: str,
    extra: str,
    conflict: bool,
) -> None:
    requirement = install_req_from_line(root, constraint=constraint)
    source = install_req_from_line(f"source[{extra}]" if extra else "source")
    factory.collect_root_requirements([source, requirement])
    dist = get_metadata_distribution(
        (
            "Metadata-Version: 2.6\nName: source\nVersion: 1\n"
            "Provides-Extra: speed\nDynamic: Requires-Dist\n"
            f"Requires-Dist: {dependency}\n"
        ).encode(),
        "source-1.tar.gz",
        "source",
    )
    linked = install_req_from_line("source @ https://example.invalid/source-1.tar.gz")

    assert factory.reject_static_dependencies(linked, dist) == conflict


def test_cached_link_name_does_not_make_unrelated_extras_mandatory(
    factory: Factory, tmp_path: Path
) -> None:
    wheel = Path(make_wheel("cached_name", "1.0").save_to_dir(tmp_path))
    template = install_req_from_line(f"cached-name @ {wheel.as_uri()}")
    assert template.link is not None
    factory._make_base_candidate_from_link(
        template.link, template, canonicalize_name("cached-name"), None
    )
    renamed = install_req_from_line(f"other-name[speed] @ {wheel.as_uri()}")
    collected = factory.collect_root_requirements(
        [renamed, install_req_from_line("dependency==1")]
    )
    assert "cached-name[speed]" in {
        requirement.name for requirement in collected.requirements
    }
    dist = get_metadata_distribution(
        b"Metadata-Version: 2.6\nName: other-name\nVersion: 1\n"
        b"Provides-Extra: speed\nRequires-Dist: dependency==2; extra == 'speed'\n",
        "other_name-1.tar.gz",
        "other-name",
    )
    linked = install_req_from_line(
        "other-name @ https://example.invalid/other_name-1.tar.gz"
    )

    assert not factory.reject_static_dependencies(linked, dist)


@pytest.mark.parametrize("constraint, conflict", [(False, False), (True, True)])
def test_linked_root_filename_version_is_not_an_enforced_specifier(
    factory: Factory, constraint: bool, conflict: bool
) -> None:
    requirement = install_req_from_line(
        "https://example.invalid/dependency-1-py3-none-any.whl", constraint=constraint
    )
    factory._set_mandatory_root_requirements([requirement])
    dist = get_metadata_distribution(
        b"Metadata-Version: 2.6\nName: source\nVersion: 1\n"
        b"Requires-Dist: dependency==2\n",
        "source-1.tar.gz",
        "source",
    )
    linked = install_req_from_line("source @ https://example.invalid/source-1.tar.gz")

    assert factory.reject_static_dependencies(linked, dist) == conflict


def test_linked_root_extras_need_actual_candidate_provenance(factory: Factory) -> None:
    factory._set_mandatory_root_requirements(
        [
            install_req_from_line(
                "source[speed] @ https://example.invalid/source-1.tar.gz"
            ),
            install_req_from_line("dependency==1"),
        ]
    )
    dist = get_metadata_distribution(
        b"Metadata-Version: 2.6\nName: source\nVersion: 1\nProvides-Extra: speed\n"
        b"Requires-Dist: dependency==2; extra == 'speed'\n",
        "source-1.tar.gz",
        "source",
    )
    linked = install_req_from_line("source @ https://example.invalid/source-1.tar.gz")

    assert not factory.reject_static_dependencies(linked, dist)


@pytest.mark.parametrize(
    "root, dependency, conflict",
    [
        ("source<2", "", True),
        ("source>=2", "", False),
        ("source===2", "", False),
        ("source", "source<2", True),
        ("source", "source>=2", False),
        ("source", "source===2", False),
    ],
)
def test_known_source_identity_points(
    factory: Factory, root: str, dependency: str, conflict: bool
) -> None:
    factory.collect_root_requirements([install_req_from_line(root)])
    metadata = "Metadata-Version: 2.2\nName: source\nVersion: 2.0\n"
    if dependency:
        metadata += f"Requires-Dist: {dependency}\n"
    dist = get_metadata_distribution(metadata.encode(), "source-2.tar.gz", "source")
    linked = install_req_from_line("source @ https://example.invalid/source-2.tar.gz")

    assert factory.reject_static_dependencies(linked, dist) == conflict


def test_reusing_factory_does_not_reuse_root_dependent_partial_metadata(
    factory: Factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "cache_demo-1.0.tar.gz"
    files = {
        "PKG-INFO": (
            "Metadata-Version: 2.6\nName: cache-demo\nVersion: 1.0\n"
            "Dynamic: Requires-Dist\nRequires-Dist: dependency==2.0\n"
        ),
        "pyproject.toml": (
            '[build-system]\nrequires = []\nbuild-backend = "backend"\n'
        ),
    }
    with tarfile.open(archive, "w:gz") as tar:
        for name, contents in files.items():
            data = contents.encode()
            member = tarfile.TarInfo(f"cache_demo-1.0/{name}")
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))

    def backend_required(*args: object, **kwargs: object) -> None:
        raise RuntimeError("complete backend metadata required")

    monkeypatch.setattr(
        "pip._internal.utils.subprocess.call_subprocess", backend_required
    )
    factory.preparer.static_metadata_fields = SDIST_RESOLUTION_FIELDS
    factory.preparer.static_dependency_rejector = factory.reject_static_dependencies
    template = install_req_from_line(archive.as_uri())
    assert template.link is not None
    roots = [
        install_req_from_line("cache-demo"),
        install_req_from_line("dependency==1"),
    ]
    factory.collect_root_requirements(roots)
    candidate = factory._make_base_candidate_from_link(
        template.link, template, canonicalize_name("cache-demo"), Version("1.0")
    )
    assert candidate is not None

    factory.collect_root_requirements(
        [install_req_from_line("cache-demo"), install_req_from_line("dependency==2")]
    )
    with pytest.raises(RuntimeError, match="complete backend metadata required"):
        factory._make_base_candidate_from_link(
            template.link, template, canonicalize_name("cache-demo"), Version("1.0")
        )
