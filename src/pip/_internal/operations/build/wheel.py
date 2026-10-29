from __future__ import annotations

import logging
import os
from email.message import Message
from email.parser import BytesParser
from zipfile import BadZipFile, ZipFile

from pip._vendor.packaging.utils import InvalidName, canonicalize_name
from pip._vendor.packaging.version import InvalidVersion, Version
from pip._vendor.pyproject_hooks import BuildBackendHookCaller

from pip._internal.exceptions import (
    InvalidWheel,
    InvalidWheelFilename,
    UnsupportedWheel,
)
from pip._internal.metadata import (
    BaseDistribution,
    FilesystemWheel,
    get_metadata_distribution,
    get_wheel_distribution,
)
from pip._internal.models.wheel import Wheel
from pip._internal.utils.subprocess import runner_with_spinner_message
from pip._internal.utils.wheel import read_wheel_metadata_file, wheel_dist_info_dir

logger = logging.getLogger(__name__)


def verify_built_wheel(
    wheel_path: str, expected_name: str | None = None, *, verify: bool = True
) -> BaseDistribution:
    """Read built metadata, optionally checking filename and metadata version."""
    message: Message
    if verify:
        wheel = Wheel(os.path.basename(wheel_path))
        if expected_name is not None and wheel.name != canonicalize_name(expected_name):
            raise InvalidWheelFilename(
                f"Wheel has unexpected file name: expected {expected_name!r}, "
                f"got {wheel.name!r}"
            )
        dist = get_wheel_distribution(FilesystemWheel(wheel_path), wheel.name)
        try:
            message = dist.metadata
        except UnicodeDecodeError as exc:
            raise UnsupportedWheel(f"Built wheel has invalid metadata: {exc}") from exc
    else:
        try:
            with ZipFile(wheel_path) as archive:
                info_dir = wheel_dist_info_dir(archive, None)
                contents = read_wheel_metadata_file(archive, f"{info_dir}/METADATA")
            contents.decode("utf-8")
        except (BadZipFile, OSError) as exc:
            raise InvalidWheel(wheel_path, expected_name or "unknown") from exc
        except UnicodeDecodeError as exc:
            raise UnsupportedWheel(f"Built wheel has invalid metadata: {exc}") from exc
        message = BytesParser().parsebytes(contents)
    try:
        names = message.get_all("Name", [])
        versions = message.get_all("Version", [])
        if len(names) != 1 or len(versions) != 1:
            raise UnsupportedWheel("Built wheel must have one Name and Version")
        name = canonicalize_name(str(names[0]), validate=True)
        version = Version(str(versions[0]))
    except (InvalidName, InvalidVersion, UnicodeDecodeError) as exc:
        raise UnsupportedWheel(f"Built wheel has invalid metadata: {exc}") from exc
    if not verify:
        return get_metadata_distribution(contents, os.path.basename(wheel_path), name)
    if name != wheel.name:
        raise InvalidWheelFilename(
            f"Wheel has unexpected file name: expected {name!r}, got {wheel.name!r}"
        )
    if version != Version(wheel.version) or dist.version != version:
        raise InvalidWheelFilename(
            f"Wheel has unexpected file name: expected {str(version)!r}, "
            f"got {wheel.version!r}"
        )
    metadata_versions = dist.metadata.get_all("Metadata-Version", [])
    if not metadata_versions:
        raise UnsupportedWheel("Missing Metadata-Version")
    if len(metadata_versions) != 1:
        raise UnsupportedWheel("Built wheel must have one Metadata-Version")
    metadata_version = str(metadata_versions[0])
    try:
        Version(metadata_version)
    except InvalidVersion as exc:
        raise UnsupportedWheel(f"Invalid Metadata-Version: {metadata_version}") from exc
    return dist


def build_wheel_pep517(
    name: str,
    backend: BuildBackendHookCaller,
    metadata_directory: str | None,
    wheel_directory: str,
) -> str | None:
    """Build one InstallRequirement using the PEP 517 build process.

    Returns path to wheel if successfully built. Otherwise, returns None.
    """
    try:
        logger.debug("Destination directory: %s", wheel_directory)

        runner = runner_with_spinner_message(
            f"Building wheel for {name} (pyproject.toml)"
        )
        with backend.subprocess_runner(runner):
            wheel_name = backend.build_wheel(
                wheel_directory=wheel_directory,
                metadata_directory=metadata_directory,
            )
    except Exception:
        logger.error("Failed building wheel for %s", name)
        return None
    return os.path.join(wheel_directory, wheel_name)
