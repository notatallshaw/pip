from functools import lru_cache
from typing import Union

from pip._vendor.packaging.utils import Version
from pip._vendor.packaging.utils import canonicalize_name as _canonicalize_name
from pip._vendor.packaging.utils import canonicalize_version as _canonicalize_version


@lru_cache(maxsize=None)
def canonicalize_version(version: Union[Version, str]) -> str:
    """
    A cached version of packaging.utils.canonicalize_version
    """
    return _canonicalize_version(version)

@lru_cache(maxsize=None)
def canonicalize_name(version: Union[Version, str]) -> str:
    """
    A cached version of packaging.utils.canonicalize_name
    """
    return _canonicalize_name(version)
