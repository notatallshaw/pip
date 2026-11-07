"""Report invalid dependency metadata when pip rejects a candidate version."""

import logging

from pip._vendor.packaging.version import Version

from pip._internal.exceptions import MetadataInvalid

logger = logging.getLogger(__name__)


def warn_invalid_metadata(version: Version, error: MetadataInvalid) -> None:
    """Report the metadata error when pip excludes a candidate version."""
    logger.warning(
        "Ignoring version %s of %s since it has invalid metadata:\n"
        "%s\n"
        "Please use pip<24.1 if you need to use this version.",
        version,
        error.ireq.name,
        error,
    )
