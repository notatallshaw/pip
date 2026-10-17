from pathlib import Path
from typing import NoReturn

import pytest

from pip._internal.exceptions import DistributionNotFound, InstallationError
from pip._internal.req.constructors import (
    install_req_from_line,
    install_req_from_req_string,
)
from pip._internal.req.dependency_sources import DependencySources, Source
from pip._internal.resolution.resolvelib.factory import Factory
from pip._internal.resolution.resolvelib.requirements import (
    SourceRequirement,
    SpecifierRequirement,
)

from tests.lib import make_wheel


def test_selected_source_precedes_external_url_prohibition(
    factory: Factory, tmp_path: Path
) -> None:
    wheel = Path(make_wheel("core", "1.0").save_to_dir(tmp_path))
    project = tmp_path / "pyproject.toml"
    project.write_text(f'[develop.sources]\ncore = {{path = "{wheel.name}"}}')
    factory._dependency_sources = DependencySources(project, tmp_path)
    factory._make_install_req_from_spec = install_req_from_req_string
    parent = install_req_from_line("outer @ https://files.pythonhosted.org/outer.zip")
    requirements = list(
        factory.make_requirements_from_spec(
            "core @ https://example.org/old.zip",
            parent,
        )
    )
    assert requirements
    candidate, _ = requirements[0].get_candidate_lookup()
    assert candidate is not None
    assert candidate.source_link is not None
    assert candidate.source_link.file_path == str(wheel)

    with pytest.raises(InstallationError, match="Packages installed from PyPI"):
        list(
            factory.make_requirements_from_spec(
                "other @ https://example.org/other.zip",
                parent,
            )
        )


@pytest.mark.parametrize("kind", ["index", "path", "url", "git"])
def test_source_error_reporting_does_not_query_default_indexes(
    factory: Factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    package = tmp_path / "core"
    package.mkdir()
    (package / "index.html").write_text("")
    source = Source(kind=kind, location=tmp_path.as_uri())
    ireq = install_req_from_line("core>=2")
    assert ireq.req is not None
    requirement = SourceRequirement(
        SpecifierRequirement(ireq), source, ireq.req.specifier
    )

    def forbid_default_query(project_name: str) -> NoReturn:
        raise AssertionError(f"Queried an unselected index for {project_name}")

    monkeypatch.setattr(factory._finder, "find_all_candidates", forbid_default_query)
    failure = factory._report_single_requirement_conflict(requirement, None)
    assert isinstance(failure, DistributionNotFound)
