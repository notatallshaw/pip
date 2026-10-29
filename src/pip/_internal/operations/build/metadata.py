"""Metadata generation logic for source distributions."""

import os

from pip._vendor.packaging.requirements import InvalidRequirement
from pip._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
from pip._vendor.packaging.utils import InvalidName, canonicalize_name
from pip._vendor.packaging.version import InvalidVersion, Version
from pip._vendor.pyproject_hooks import BuildBackendHookCaller

from pip._internal.build_env import BuildEnvironment
from pip._internal.exceptions import (
    InstallationSubprocessError,
    MetadataGenerationFailed,
    UnsupportedWheel,
)
from pip._internal.metadata import BaseDistribution
from pip._internal.utils.packaging import get_requirement
from pip._internal.utils.subprocess import runner_with_spinner_message
from pip._internal.utils.temp_dir import TempDirectory


def generate_metadata(
    build_env: BuildEnvironment, backend: BuildBackendHookCaller, details: str
) -> str:
    """Generate metadata using mechanisms described in PEP 517.

    Returns the generated metadata directory.
    """
    metadata_tmpdir = TempDirectory(kind="modern-metadata", globally_managed=True)

    metadata_dir = metadata_tmpdir.path

    with build_env:
        # Note that BuildBackendHookCaller implements a fallback for
        # prepare_metadata_for_build_wheel, so we don't have to
        # consider the possibility that this hook doesn't exist.
        runner = runner_with_spinner_message("Preparing metadata (pyproject.toml)")
        with backend.subprocess_runner(runner):
            try:
                distinfo_dir = backend.prepare_metadata_for_build_wheel(metadata_dir)
            except InstallationSubprocessError as error:
                raise MetadataGenerationFailed(package_details=details) from error

    return os.path.join(metadata_dir, distinfo_dir)


def check_static_sdist_metadata(
    source_dist: BaseDistribution, prepared_dist: BaseDistribution
) -> None:
    """Check static sdist promises against completed distribution metadata."""
    dynamic = {field.lower() for field in source_dist.metadata.get_all("Dynamic", [])}
    append_only = Version(source_dist.metadata_version or "0") >= Version("2.6")
    names = prepared_dist.metadata.get_all("Name", [])
    versions = prepared_dist.metadata.get_all("Version", [])
    if len(names) != 1 or len(versions) != 1:
        raise UnsupportedWheel("Prepared metadata must have one Name and Version")
    try:
        name = canonicalize_name(str(names[0]), validate=True)
        version = Version(str(versions[0]))
    except (InvalidName, InvalidVersion) as exc:
        raise UnsupportedWheel(
            f"Prepared metadata has invalid identity: {exc}"
        ) from exc
    comparisons: list[tuple[str, object, object]] = [
        ("Name", source_dist.canonical_name, name),
        ("Version", source_dist.version, version),
    ]
    if "requires-python" not in dynamic:
        try:
            prepared_python = SpecifierSet(
                str(prepared_dist.metadata.get("Requires-Python", ""))
            )
            source_python = source_dist.requires_python
        except InvalidSpecifier as exc:
            raise UnsupportedWheel(f"Invalid Requires-Python: {exc}") from exc
        comparisons.append(("Requires-Python", source_python, prepared_python))
    # Completed metadata is available even when resolution ignored dependencies.
    try:
        prepared_requires = frozenset(
            get_requirement(raw.strip())
            for raw in prepared_dist.iter_raw_dependencies()
        )
        if "requires-dist" not in dynamic or append_only:
            source_requires = frozenset(
                get_requirement(raw.strip())
                for raw in source_dist.iter_raw_dependencies()
            )
            if "requires-dist" in dynamic:
                if not source_requires <= prepared_requires:
                    raise UnsupportedWheel(
                        "Prepared metadata dropped a fixed Requires-Dist from the sdist"
                    )
            else:
                comparisons.append(
                    ("Requires-Dist", source_requires, prepared_requires)
                )
    except InvalidRequirement as exc:
        raise UnsupportedWheel(f"Invalid Requires-Dist: {exc}") from exc
    if "provides-extra" not in dynamic or append_only:
        source_extras = frozenset(source_dist.iter_provided_extras())
        prepared_extras = frozenset(prepared_dist.iter_provided_extras())
        if "provides-extra" in dynamic:
            if not source_extras <= prepared_extras:
                raise UnsupportedWheel(
                    "Prepared metadata dropped a fixed Provides-Extra from the sdist"
                )
        else:
            comparisons.append(("Provides-Extra", source_extras, prepared_extras))
    for field, source_value, wheel_value in comparisons:
        if source_value != wheel_value:
            raise UnsupportedWheel(
                f"Prepared metadata has different {field} "
                "from the sdist's static metadata"
            )
