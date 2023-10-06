
from functools import lru_cache
from typing import Union

from pip._vendor.packaging.version import LegacyVersion, Version, parse

__all__ = ["parse_version"]


@lru_cache(maxsize=None)
def parse_version(version: str) -> Union[LegacyVersion, Version]:
    """
    A cached call the the packaging.version.parse function
    """
    return parse(version)

@lru_cache(maxsize=None)
def parse_new_version(version: str) -> Version:
    """
    A cached call the the packaging.version.parse function
    """
    return Version(version)

