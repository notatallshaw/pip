from pathlib import Path

import pytest

from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import Version

from pip._internal.cache import WheelCache
from pip._internal.exceptions import InstallationError
from pip._internal.index.package_finder import PackageFinder
from pip._internal.metadata import BaseDistribution
from pip._internal.models.link import Link
from pip._internal.operations.prepare import RequirementPreparer
from pip._internal.req.constructors import (
    install_req_from_line,
    install_req_from_req_string,
)
from pip._internal.req.req_install import InstallRequirement
from pip._internal.resolution.resolvelib.candidates import LinkCandidate
from pip._internal.resolution.resolvelib.factory import Factory

from tests.lib.wheel import make_wheel


def _parent_candidate(
    finder: PackageFinder,
    preparer: RequirementPreparer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: Link,
    *,
    cached: bool,
    dependency: str = "child",
) -> LinkCandidate:
    """Prepare a real candidate, supplying remote metadata at the IO boundary."""
    wheel = make_wheel(
        "parent",
        "1.0",
        metadata=(
            "Metadata-Version: 2.1\nName: parent\nVersion: 1.0\n"
            f"Requires-Dist: {dependency}\n"
        ),
    )
    cache = WheelCache(str(tmp_path / "cache"))
    if cached:
        directory = Path(cache.get_path_for_link(source))
        directory.mkdir(parents=True)
        wheel.save_to_dir(directory)
    else:
        prepare_linked = preparer.prepare_linked_requirement

        def prepare_remote(
            req: InstallRequirement, parallel_builds: bool
        ) -> BaseDistribution:
            if req.link == source:
                return wheel.as_distribution("parent")
            return prepare_linked(req, parallel_builds=parallel_builds)

        monkeypatch.setattr(preparer, "prepare_linked_requirement", prepare_remote)

    factory = Factory(
        finder=finder,
        preparer=preparer,
        make_install_req=install_req_from_req_string,
        wheel_cache=cache,
        use_user_site=False,
        force_reinstall=False,
        ignore_installed=True,
        ignore_requires_python=False,
        py_version_info=None,
    )
    candidate = factory._make_base_candidate_from_link(
        source,
        install_req_from_line("parent==1.0"),
        canonicalize_name("parent"),
        Version("1.0"),
    )
    assert isinstance(candidate, LinkCandidate)
    assert candidate.source_link == source
    parent = candidate.get_install_requirement()
    assert parent is not None
    if cached:
        assert parent.link is not None
        assert parent.link.is_file
        assert parent.cached_wheel_source_link == source
    else:
        assert parent.link == source
        assert parent.cached_wheel_source_link is None
    return candidate


@pytest.mark.parametrize(
    "domain", ["files.pythonhosted.org", "test-files.pythonhosted.org"]
)
@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("target", ["example.org", "files.pythonhosted.org"])
def test_pypi_parent_rejects_dependency_urls(
    finder: PackageFinder,
    preparer: RequirementPreparer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    domain: str,
    cached: bool,
    target: str,
) -> None:
    candidate = _parent_candidate(
        finder,
        preparer,
        tmp_path,
        monkeypatch,
        Link(f"https://{domain}/parent-1.0.tar.gz"),
        cached=cached,
    )
    dependency = f"child @ https://{target}/child-1.0-py3-none-any.whl"
    with pytest.raises(InstallationError) as error:
        install_req_from_req_string(dependency, candidate.get_install_requirement())
    assert str(error.value) == (
        "Packages installed from PyPI cannot depend on packages "
        "which are not also hosted on PyPI.\n"
        f"parent depends on {dependency} "
    )


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("cached", [False, True])
def test_other_parent_sources_allow_dependency_urls(
    finder: PackageFinder,
    preparer: RequirementPreparer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local: bool,
    cached: bool,
) -> None:
    url = (
        (tmp_path / "parent-1.0.tar.gz").as_uri()
        if local
        else "https://example.org/parent-1.0.tar.gz"
    )
    candidate = _parent_candidate(
        finder,
        preparer,
        tmp_path,
        monkeypatch,
        Link(url),
        cached=cached,
    )
    dependency = "child @ https://example.org/child-1.0-py3-none-any.whl"
    requirement = install_req_from_req_string(
        dependency, candidate.get_install_requirement()
    )
    assert requirement.req is not None
    assert str(requirement.req) == dependency


def test_cached_pypi_parent_keeps_named_dependencies_and_parse_errors(
    finder: PackageFinder,
    preparer: RequirementPreparer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _parent_candidate(
        finder,
        preparer,
        tmp_path,
        monkeypatch,
        Link("https://files.pythonhosted.org/parent-1.0.tar.gz"),
        cached=True,
    )
    parent = candidate.get_install_requirement()
    requirement = install_req_from_req_string("child>=1", parent)
    assert requirement.req is not None
    assert str(requirement.req) == "child>=1"

    with pytest.raises(InstallationError, match="Invalid requirement:"):
        install_req_from_req_string("child @", parent)


@pytest.mark.parametrize(
    "source_url, rejected",
    [
        ("https://files.pythonhosted.org/parent-1.0.tar.gz", True),
        ("https://test-files.pythonhosted.org/parent-1.0.tar.gz", True),
        ("https://example.org/parent-1.0.tar.gz", False),
        (None, False),
    ],
)
def test_cached_candidate_dependencies_use_original_source(
    finder: PackageFinder,
    preparer: RequirementPreparer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_url: str | None,
    rejected: bool,
) -> None:
    child = Path(make_wheel("child", "1.0").save_to_dir(tmp_path))
    dependency = f"child @ {child.as_uri()}"
    source = Link(source_url or (tmp_path / "parent-1.0.tar.gz").as_uri())
    candidate = _parent_candidate(
        finder,
        preparer,
        tmp_path,
        monkeypatch,
        source,
        cached=True,
        dependency=dependency,
    )
    if rejected:
        with pytest.raises(InstallationError, match="Packages installed from PyPI"):
            list(candidate.iter_dependencies(with_requires=True))
    else:
        requirements = [
            req
            for req in candidate.iter_dependencies(with_requires=True)
            if req is not None
        ]
        assert len(requirements) == 1
        dependency_candidate, _ = requirements[0].get_candidate_lookup()
        assert dependency_candidate is not None
        assert dependency_candidate.source_link == Link(child.as_uri())
