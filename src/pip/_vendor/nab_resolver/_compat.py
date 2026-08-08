"""Typing shims for the interpreter range nab-resolver supports.

nab-resolver declares no runtime dependencies, so anything ``typing`` grew
after the 3.10 floor is defined here rather than taken from a backport
package.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable
from typing import Any, TypeVar

__all__ = [
    "override",
]

_F = TypeVar("_F", bound=Callable[..., Any])

if sys.version_info >= (3, 12):  # pragma: no cover
    from typing import override
else:  # pragma: no cover

    def override(method: _F, /) -> _F:
        """Declare that ``method`` overrides a method in a base class.

        Same contract as :func:`typing.override` on 3.12 and newer: the
        type checkers do the checking, and ``__override__`` is set only so
        the decoration can be seen by introspection.
        """
        marked: Any = method
        with contextlib.suppress(AttributeError, TypeError):
            marked.__override__ = True
        return method
