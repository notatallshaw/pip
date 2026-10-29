"""Orchestrator for building wheels from InstallRequirements."""

from __future__ import annotations

import logging
import os.path
import re
import shutil
from collections.abc import Iterable
from tempfile import TemporaryDirectory

from pip._vendor.packaging.requirements import InvalidRequirement
from pip._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
from pip._vendor.packaging.version import Version

from pip._internal.cache import WheelCache
from pip._internal.exceptions import (
    InvalidWheel,
    InvalidWheelFilename,
    UnsupportedWheel,
)
from pip._internal.metadata import (
    BaseDistribution,
)
from pip._internal.models.link import Link
from pip._internal.operations.build.wheel import build_wheel_pep517, verify_built_wheel
from pip._internal.operations.build.wheel_editable import build_wheel_editable
from pip._internal.req.req_install import InstallRequirement
from pip._internal.utils.logging import indent_log
from pip._internal.utils.misc import ensure_dir, hash_file
from pip._internal.utils.packaging import get_requirement
from pip._internal.utils.urls import path_to_url
from pip._internal.vcs import vcs

logger = logging.getLogger(__name__)

_egg_info_re = re.compile(r"([a-z0-9_.]+)-([a-z0-9_.!+-]+)", re.IGNORECASE)

BuildResult = tuple[list[InstallRequirement], list[InstallRequirement]]


def _contains_egg_info(s: str) -> bool:
    """Determine whether the string looks like an egg_info.

    :param s: The string to parse. E.g. foo-2.1
    """
    return bool(_egg_info_re.search(s))


def _should_cache(
    req: InstallRequirement,
) -> bool | None:
    """
    Return whether a built InstallRequirement can be stored in the persistent
    wheel cache, assuming the wheel cache is available.
    """
    if req.editable or not req.source_dir:
        # never cache editable requirements
        return False
    if req.link and req.link.is_existing_dir():
        # never cache local directory requirements
        return False

    if req.link and req.link.is_vcs:
        # VCS checkout. Do not cache
        # unless it points to an immutable commit hash.
        assert not req.editable
        assert req.source_dir
        vcs_backend = vcs.get_backend_for_scheme(req.link.scheme)
        assert vcs_backend
        if vcs_backend.is_immutable_rev_checkout(req.link.url, req.source_dir):
            return True
        return False

    assert req.link
    base, ext = req.link.splitext()
    if _contains_egg_info(base):
        return True

    # Otherwise, do not cache.
    return False


def _get_cache_dir(
    req: InstallRequirement,
    wheel_cache: WheelCache,
) -> str:
    """Return the persistent or temporary cache directory where the built
    wheel need to be stored.
    """
    cache_available = bool(wheel_cache.cache_dir)
    assert req.link
    if cache_available and _should_cache(req):
        cache_dir = wheel_cache.get_path_for_link(req.link)
    else:
        cache_dir = wheel_cache.get_ephem_path_for_link(req.link)
    return cache_dir


def _verify_one(req: InstallRequirement, wheel_path: str) -> None:
    verify_built_wheel(wheel_path, expected_name=req.name or "")


def _check_static_sdist_metadata(
    source_dist: BaseDistribution, wheel_dist: BaseDistribution
) -> None:
    """Check the metadata promises used to resolve an sdist before building it."""
    dynamic = {field.lower() for field in source_dist.metadata.get_all("Dynamic", [])}
    append_only = Version(source_dist.metadata_version or "0") >= Version("2.6")
    comparisons: list[tuple[str, object, object]] = [
        ("Name", source_dist.canonical_name, wheel_dist.canonical_name),
        ("Version", source_dist.version, wheel_dist.version),
    ]
    if "requires-python" not in dynamic:
        try:
            wheel_python = SpecifierSet(
                str(wheel_dist.metadata.get("Requires-Python", ""))
            )
        except InvalidSpecifier as exc:
            raise UnsupportedWheel(f"Invalid Requires-Python: {exc}") from exc
        comparisons.append(
            ("Requires-Python", source_dist.requires_python, wheel_python)
        )
    # The built metadata is now available, even if resolution ignored dependencies.
    try:
        wheel_requires = frozenset(
            get_requirement(raw.strip()) for raw in wheel_dist.iter_raw_dependencies()
        )
        if "requires-dist" not in dynamic or append_only:
            source_requires = frozenset(
                get_requirement(raw.strip())
                for raw in source_dist.iter_raw_dependencies()
            )
            if "requires-dist" in dynamic:
                if not source_requires <= wheel_requires:
                    raise UnsupportedWheel(
                        "Built wheel dropped a fixed Requires-Dist from the sdist"
                    )
            else:
                comparisons.append(("Requires-Dist", source_requires, wheel_requires))
    except InvalidRequirement as exc:
        raise UnsupportedWheel(f"Invalid Requires-Dist: {exc}") from exc
    if "provides-extra" not in dynamic or append_only:
        source_extras = frozenset(source_dist.iter_provided_extras())
        wheel_extras = frozenset(wheel_dist.iter_provided_extras())
        if "provides-extra" in dynamic:
            if not source_extras <= wheel_extras:
                raise UnsupportedWheel(
                    "Built wheel dropped a fixed Provides-Extra from the sdist"
                )
        else:
            comparisons.append(("Provides-Extra", source_extras, wheel_extras))
    for field, source_value, wheel_value in comparisons:
        if source_value != wheel_value:
            raise UnsupportedWheel(
                f"Built wheel has different {field} from the sdist's static metadata"
            )


def _build_one(
    req: InstallRequirement,
    output_dir: str,
    verify: bool,
    editable: bool,
) -> str | None:
    """Build one wheel.

    :return: The filename of the built wheel, or None if the build failed.
    """
    artifact = "editable" if editable else "wheel"
    try:
        ensure_dir(output_dir)
    except OSError as e:
        logger.warning(
            "Building %s for %s failed: %s",
            artifact,
            req.name,
            e,
        )
        return None

    # Install build deps into temporary directory (PEP 518)
    with req.build_env:
        wheel_path = _build_one_inside_env(req, output_dir, editable)
    wheel_dist = None
    if wheel_path and req.static_sdist_metadata is not None:
        try:
            wheel_dist = verify_built_wheel(
                wheel_path, expected_name=req.name if verify else None, verify=verify
            )
            _check_static_sdist_metadata(req.static_sdist_metadata, wheel_dist)
        except (InvalidWheel, InvalidWheelFilename, UnsupportedWheel) as exc:
            logger.warning("Built %s for %s is invalid: %s", artifact, req.name, exc)
            # A rejected build must not become a reusable cached wheel.
            os.unlink(wheel_path)
            return None
    if wheel_path and verify and wheel_dist is None:
        try:
            _verify_one(req, wheel_path)
        except (InvalidWheelFilename, UnsupportedWheel) as e:
            logger.warning("Built %s for %s is invalid: %s", artifact, req.name, e)
            if (
                req.static_sdist_metadata is not None
                or req.prebuilt_wheel_path is not None
            ):
                os.unlink(wheel_path)
            return None
    if wheel_dist is not None:
        req.set_dist(wheel_dist)
    return wheel_path


def _build_one_inside_env(
    req: InstallRequirement,
    output_dir: str,
    editable: bool,
) -> str | None:
    with TemporaryDirectory(dir=output_dir) as wheel_directory:
        assert req.name
        assert req.pep517_backend
        wheel_path: str | None
        if req.prebuilt_wheel_path is not None:
            # Retain the one real wheel already used to discover source metadata.
            wheel_path = os.path.join(
                wheel_directory, os.path.basename(req.prebuilt_wheel_path)
            )
            shutil.copyfile(req.prebuilt_wheel_path, wheel_path)
        elif editable:
            assert req.metadata_directory
            wheel_path = build_wheel_editable(
                name=req.name,
                backend=req.pep517_backend,
                metadata_directory=req.metadata_directory,
                wheel_directory=wheel_directory,
            )
        else:
            wheel_path = build_wheel_pep517(
                name=req.name,
                backend=req.pep517_backend,
                metadata_directory=req.metadata_directory,
                wheel_directory=wheel_directory,
            )

        if wheel_path is not None:
            wheel_name = os.path.basename(wheel_path)
            dest_path = os.path.join(output_dir, wheel_name)
            try:
                wheel_hash, length = hash_file(wheel_path)
                # We can do a replace here because wheel_path is guaranteed to
                # be in the same filesystem as output_dir. This will perform an
                # atomic rename, which is necessary to avoid concurrency issues
                # when populating the cache.
                os.replace(wheel_path, dest_path)
                logger.info(
                    "Created wheel for %s: filename=%s size=%d sha256=%s",
                    req.name,
                    wheel_name,
                    length,
                    wheel_hash.hexdigest(),
                )
                logger.info("Stored in directory: %s", output_dir)
                return dest_path
            except Exception as e:
                logger.warning(
                    "Building wheel for %s failed: %s",
                    req.name,
                    e,
                )
        return None


def build(
    requirements: Iterable[InstallRequirement],
    wheel_cache: WheelCache,
    verify: bool,
    allow_editables: bool,
) -> BuildResult:
    """Build wheels.

    :return: The list of InstallRequirement that succeeded to build and
        the list of InstallRequirement that failed to build.
    """
    if not requirements:
        return [], []

    # Build the wheels.
    logger.info(
        "Building wheels for collected packages: %s",
        ", ".join(req.name for req in requirements),  # type: ignore
    )

    with indent_log():
        build_successes, build_failures = [], []
        for req in requirements:
            assert req.name
            cache_dir = _get_cache_dir(req, wheel_cache)
            wheel_file = _build_one(
                req,
                cache_dir,
                verify,
                req.editable and allow_editables,
            )
            if wheel_file:
                # Record the download origin in the cache
                if req.download_info is not None:
                    # download_info is guaranteed to be set because when we build an
                    # InstallRequirement it has been through the preparer before, but
                    # let's be cautious.
                    wheel_cache.record_download_origin(cache_dir, req.download_info)
                # Update the link for this.
                req.link = Link(path_to_url(wheel_file))
                req.local_file_path = req.link.file_path
                assert req.link.is_wheel
                build_successes.append(req)
            else:
                build_failures.append(req)

    # notify success/failure
    if build_successes:
        logger.info(
            "Successfully built %s",
            " ".join([req.name for req in build_successes]),  # type: ignore
        )
    if build_failures:
        logger.info(
            "Failed to build %s",
            " ".join([req.name for req in build_failures]),  # type: ignore
        )
    # Return a list of requirements that failed to build
    return build_successes, build_failures
