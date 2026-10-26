from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from contextlib import nullcontext
from email.parser import BytesParser
from pathlib import Path
from typing import TYPE_CHECKING

from pip._vendor.packaging.metadata import parse_email
from pip._vendor.packaging.utils import (
    InvalidName,
    InvalidSdistFilename,
    NormalizedName,
    canonicalize_name,
    parse_sdist_filename,
)
from pip._vendor.packaging.version import InvalidVersion, Version

from pip._internal.build_env import (
    BuildIsolationMode,
    NoOpBuildEnvironment,
    VenvBuildEnvironment,
    VirtualBuildEnvironment,
)
from pip._internal.distributions.base import AbstractDistribution
from pip._internal.exceptions import InstallationError
from pip._internal.metadata import BaseDistribution, get_metadata_distribution
from pip._internal.utils.subprocess import runner_with_spinner_message

if TYPE_CHECKING:
    from pip._internal.build_env import BuildEnvironmentInstaller

logger = logging.getLogger(__name__)

SDIST_RESOLUTION_FIELDS = frozenset(
    {"requires-python", "requires-dist", "provides-extra"}
)


def parse_sdist_identity(
    filename: str, *, require_normalized: bool = True
) -> tuple[NormalizedName, Version] | None:
    """Parse an sdist identity, optionally requiring filename normalization."""
    if not filename.endswith(".tar.gz") or filename[:-7].count("-") != 1:
        return None
    name_part, version_part = filename[:-7].split("-")
    try:
        name, version = parse_sdist_filename(filename)
        canonicalize_name(name_part, validate=True)
    except (InvalidName, InvalidSdistFilename):
        return None
    if require_normalized and (
        name_part != name.replace("-", "_") or version_part != str(version)
    ):
        return None
    return name, version


def read_sdist_metadata(
    source_dir: str, filename: str, required_fields: frozenset[str]
) -> BaseDistribution | None:
    """Read fixed metadata from the root of an unpacked source distribution."""
    # PKG-INFO confirms identity for historical filename forms.
    identity = parse_sdist_identity(filename, require_normalized=False)
    if identity is None:
        return None
    try:
        contents = Path(source_dir, "PKG-INFO").read_bytes()
        contents.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    message = BytesParser().parsebytes(contents)
    if message.defects:
        return None
    raw, unparsed = parse_email(contents)
    metadata_version = raw.get("metadata_version", "")
    # Older sdists have no static-field guarantee; a new major may change it.
    if re.fullmatch(r"2\.(?:[2-9]|[1-9][0-9]+)", metadata_version) is None:
        return None
    identity_fields = {"metadata-version", "name", "version"}
    if unparsed.keys() & (identity_fields | required_fields | {"dynamic"}):
        return None
    if "*" in required_fields and unparsed:
        return None
    try:
        name = canonicalize_name(raw["name"], validate=True)
        version = Version(raw["version"])
    except (KeyError, InvalidName, InvalidVersion):
        return None
    if (name, version) != identity:
        return None

    dynamic = {field.lower() for field in raw.get("dynamic", [])}
    if any(not field.replace("-", "").isalnum() for field in dynamic):
        return None
    if dynamic & identity_fields:
        return None
    if dynamic & required_fields or ("*" in required_fields and dynamic):
        return None

    # Since 2.6, dynamic dependency and extras lists retain their fixed prefixes.
    fixed_prefixes = (
        {"requires-dist", "provides-extra"}
        if Version(metadata_version) >= Version("2.6")
        else set()
    )
    for field in dynamic:
        if field not in fixed_prefixes:
            del message[field]
    if "description" in dynamic:
        message.set_payload("")
    static_contents = message.as_bytes() if dynamic else contents
    return get_metadata_distribution(static_contents, filename, name)


class SourceDistribution(AbstractDistribution):
    """Represents a source distribution.

    The preparation step for these needs metadata for the packages to be
    generated.
    """

    @property
    def build_tracker_id(self) -> str | None:
        """Identify this requirement uniquely by its link."""
        assert self.req.link
        return self.req.link.url_without_fragment

    def get_metadata_distribution(self) -> BaseDistribution:
        return self.req.get_dist()

    def prepare_distribution_metadata(
        self,
        build_env_installer: BuildEnvironmentInstaller,
        build_isolation: BuildIsolationMode,
        check_build_deps: bool,
        allow_editables: bool,
    ) -> None:
        # Load pyproject.toml and set up backend environment
        self.req.load_pyproject_toml()
        self._prepare_build_env(build_isolation, build_env_installer)

        # Set up the build isolation, if this requirement should be isolated
        if build_isolation != "off":
            # Setup an isolated environment and install the build backend static
            # requirements in it.
            self._prepare_build_backend(build_isolation)
            # Check that the build backend supports PEP 660. This cannot be done
            # earlier because we need to setup the build backend to verify it
            # supports build_editable, nor can it be done later, because we want
            # to avoid installing build requirements needlessly.
            self.req.editable_sanity_check()
            # Install the dynamic build requirements.
            self._install_build_reqs(
                build_env_installer, build_isolation, allow_editables=allow_editables
            )
        else:
            # When not using build isolation, we still need to check that
            # the build backend supports PEP 660.
            self.req.editable_sanity_check()
        # Check if the current environment provides build dependencies
        if check_build_deps:
            pyproject_requires = self.req.pyproject_requires
            assert pyproject_requires is not None
            conflicting, missing = self.req.build_env.check_requirements(
                pyproject_requires
            )
            if conflicting:
                self._raise_conflicts("the backend dependencies", conflicting)
            if missing:
                self._raise_missing_reqs(missing)
        self.req.prepare_metadata(allow_editables)

    def _prepare_build_env(
        self,
        build_isolation: BuildIsolationMode,
        build_env_installer: BuildEnvironmentInstaller,
    ) -> None:
        if build_isolation == "virtual":
            self.req.build_env = VirtualBuildEnvironment(build_env_installer)
        elif build_isolation == "venv":
            self.req.build_env = VenvBuildEnvironment(build_env_installer)

        self.req.configure_backend(self.req.build_env.python_executable)

    def _prepare_build_backend(self, build_isolation: BuildIsolationMode) -> None:
        # Install the pyproject.toml declared build-time requirements.
        pyproject_requires = self.req.pyproject_requires
        assert pyproject_requires is not None
        assert not isinstance(self.req.build_env, NoOpBuildEnvironment)

        context = self.req.build_env if build_isolation == "venv" else nullcontext()
        with context:
            self.req.build_env.install_requirements(
                pyproject_requires,
                "overlay",
                kind="build dependencies",
                for_req=self.req,
            )
        conflicting, missing = self.req.build_env.check_requirements(
            self.req.requirements_to_check
        )
        if conflicting:
            self._raise_conflicts("PEP 517/518 supported requirements", conflicting)
        if missing:
            logger.warning(
                "Missing build requirements in pyproject.toml for %s.",
                self.req,
            )
            logger.warning(
                "The project does not specify a build backend, and "
                "pip cannot fall back to setuptools without %s.",
                " and ".join(map(repr, sorted(missing))),
            )

    def _get_build_requires_wheel(self) -> Iterable[str]:
        with self.req.build_env:
            runner = runner_with_spinner_message("Getting requirements to build wheel")
            backend = self.req.pep517_backend
            assert backend is not None
            with backend.subprocess_runner(runner):
                return backend.get_requires_for_build_wheel()

    def _get_build_requires_editable(self) -> Iterable[str]:
        with self.req.build_env:
            runner = runner_with_spinner_message(
                "Getting requirements to build editable"
            )
            backend = self.req.pep517_backend
            assert backend is not None
            with backend.subprocess_runner(runner):
                return backend.get_requires_for_build_editable()

    def _install_build_reqs(
        self,
        build_env_installer: BuildEnvironmentInstaller,
        build_isolation: BuildIsolationMode,
        allow_editables: bool,
    ) -> None:
        # Install any extra build dependencies that the backend requests.
        # This must be done in a second pass, as the pyproject.toml
        # dependencies must be installed before we can call the backend.
        if (
            self.req.editable
            and allow_editables
            and self.req.supports_pyproject_editable
        ):
            build_reqs = self._get_build_requires_editable()
        else:
            build_reqs = self._get_build_requires_wheel()
        conflicting, missing = self.req.build_env.check_requirements(build_reqs)
        if conflicting:
            self._raise_conflicts("the backend dependencies", conflicting)
        context = self.req.build_env if build_isolation == "venv" else nullcontext()
        with context:
            self.req.build_env.install_requirements(
                missing, "normal", kind="backend dependencies", for_req=self.req
            )

    def _raise_conflicts(
        self, conflicting_with: str, conflicting_reqs: set[tuple[str, str]]
    ) -> None:
        format_string = (
            "Some build dependencies for {requirement} "
            "conflict with {conflicting_with}: {description}."
        )
        error_message = format_string.format(
            requirement=self.req,
            conflicting_with=conflicting_with,
            description=", ".join(
                f"{installed} is incompatible with {wanted}"
                for installed, wanted in sorted(conflicting_reqs)
            ),
        )
        raise InstallationError(error_message)

    def _raise_missing_reqs(self, missing: set[str]) -> None:
        format_string = (
            "Some build dependencies for {requirement} are missing: {missing}."
        )
        error_message = format_string.format(
            requirement=self.req, missing=", ".join(map(repr, sorted(missing)))
        )
        raise InstallationError(error_message)
