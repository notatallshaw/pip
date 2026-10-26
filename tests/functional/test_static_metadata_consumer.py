from __future__ import annotations

import sys

import pytest

from pip._internal.build_env import SubprocessBuildEnvironmentInstaller
from pip._internal.distributions.sdist import SDIST_RESOLUTION_FIELDS
from pip._internal.exceptions import MetadataGenerationFailed, UnsupportedPythonVersion
from pip._internal.models.target_python import TargetPython
from pip._internal.network.session import PipSession
from pip._internal.operations.build.build_tracker import get_build_tracker
from pip._internal.operations.prepare import RequirementPreparer
from pip._internal.req.constructors import (
    install_req_from_line,
    install_req_from_req_string,
)
from pip._internal.resolution.resolvelib.resolver import Resolver

from tests.lib import (
    PipTestEnvironment,
    create_basic_sdist_for_package,
    make_test_finder,
)


@pytest.mark.parametrize(
    "bound, policy",
    [(True, policy) for policy in ("host", "foreign", "ignore", "restore-field")]
    + [(False, "host")],
)
def test_static_python_rejection_uses_the_consuming_resolver(
    script: PipTestEnvironment, bound: bool, policy: str
) -> None:
    event = script.scratch_path / "metadata-needed"
    minimum_host = f">={sys.version_info.major}.{sys.version_info.minor}"
    requires_python = ">=99" if policy in {"ignore", "restore-field"} else minimum_host
    dynamic = "Requires-Python" if policy == "restore-field" else "Requires-Dist"
    archive = create_basic_sdist_for_package(
        script,
        "consumer_demo",
        "1.0",
        extra_files={
            "PKG-INFO": (
                "Metadata-Version: 2.2\nName: consumer-demo\nVersion: 1.0\n"
                f"Requires-Python: {requires_python}\nDynamic: {dynamic}\n"
            ),
            "pyproject.toml": (
                '[build-system]\nrequires = []\nbuild-backend = "backend"\n'
                'backend-path = ["."]\n'
            ),
            "backend.py": (
                "from pathlib import Path\n"
                "def prepare_metadata_for_build_wheel(\n"
                "    metadata_directory, config_settings=None\n"
                "):\n"
                f"    Path({str(event)!r}).touch()\n"
                "    raise RuntimeError('complete metadata needed')\n"
            ),
        },
    )
    finder_target = (
        None if policy == "foreign" else TargetPython(py_version_info=(3, 9, 0))
    )
    resolver_target = (3, 9, 0) if policy == "foreign" else None
    session = PipSession()
    finder = make_test_finder(session=session, target_python=finder_target)
    with get_build_tracker() as tracker:
        preparer = RequirementPreparer(
            build_dir=str(script.scratch_path / "build"),
            download_dir=None,
            src_dir="",
            build_isolation="off",
            build_isolation_installer=SubprocessBuildEnvironmentInstaller(finder),
            check_build_deps=False,
            build_tracker=tracker,
            session=session,
            progress_bar="off",
            finder=finder,
            require_hashes=False,
            use_user_site=False,
            lazy_wheel=False,
            verbosity=0,
            legacy_resolver=False,
            allow_editables=False,
        )
        preparer.static_metadata_fields = SDIST_RESOLUTION_FIELDS
        if policy == "restore-field":
            preparer.static_metadata_fields -= {"requires-python"}
        requirement = install_req_from_line(archive.as_uri(), user_supplied=True)
        if not bound:
            with pytest.raises(MetadataGenerationFailed):
                preparer.prepare_linked_requirement(requirement)
        else:
            resolver = Resolver(
                preparer=preparer,
                finder=finder,
                wheel_cache=None,
                make_install_req=install_req_from_req_string,
                use_user_site=False,
                ignore_dependencies=False,
                only_dependencies=False,
                ignore_installed=True,
                ignore_requires_python=policy == "ignore",
                force_reinstall=False,
                upgrade_strategy="to-satisfy-only",
                py_version_info=resolver_target,
            )
            expected_error = (
                UnsupportedPythonVersion
                if policy == "foreign"
                else MetadataGenerationFailed
            )
            with pytest.raises(expected_error):
                resolver.resolve([requirement], check_supported_wheels=True)

    assert event.exists() == (policy != "foreign")
