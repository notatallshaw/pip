from __future__ import annotations

import functools
import sys
import time
from collections.abc import Callable, Generator, Iterable, Iterator
from typing import TYPE_CHECKING, Literal, TypeVar

from pip._internal.cli.spinners import RateLimiter
from pip._internal.cli.terminal import LiveLine, Text
from pip._internal.utils.logging import get_console, get_indentation
from pip._internal.utils.misc import format_size

if TYPE_CHECKING:
    from pip._internal.req.req_install import InstallRequirement

T = TypeVar("T")
ProgressRenderer = Callable[[Iterable[T]], Iterator[T]]
BarType = Literal["on", "off", "raw"]


def _bar(completed: int, total: int) -> str:
    """Render a compact bar for known totals."""
    width = 20
    filled = min(width, int(width * completed / total)) if total > 0 else 0
    return "[" + "=" * filled + " " * (width - filled) + "]"


def _elapsed(seconds: float) -> str:
    """Format a duration in hours, minutes and seconds."""
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}"


def _download_progress_bar(
    iterable: Iterable[bytes],
    *,
    size: int | None,
    initial_progress: int | None = None,
) -> Generator[bytes, None, None]:
    console = get_console()
    current = initial = initial_progress or 0
    start = time.monotonic()
    limiter = RateLimiter(1 / 5)
    indent = " " * (get_indentation() + 2)

    def description(*, finished: bool = False) -> str:
        """Report current bytes and throughput from this download attempt."""
        elapsed = max(time.monotonic() - start, 0)
        speed = (current - initial) / elapsed if elapsed else 0
        rate = f"{format_size(speed)}/s" if speed else "?"

        if size is not None and size > 0:
            amount = f"{format_size(current)}/{format_size(size)} {_bar(current, size)}"
            remaining = (
                _elapsed((size - current) / speed)
                if speed and size > current
                else "0:00:00"
            )
            timing = _elapsed(elapsed) if finished else f"eta {remaining}"
        else:
            amount = format_size(current)
            timing = _elapsed(elapsed)

        return f"{indent}{amount} {rate} {timing}"

    with LiveLine(console) as line:
        for chunk in iterable:
            yield chunk
            # Count a chunk only after the consumer has accepted it.
            current += len(chunk)
            if console.interactive and limiter.ready():
                line.update(description())
                limiter.reset()

    console.print(Text(description(finished=True)))


def _install_progress_bar(
    iterable: Iterable[InstallRequirement], *, total: int
) -> Iterator[InstallRequirement]:
    console = get_console()
    current = 0
    limiter = RateLimiter(1 / 6)
    indent = " " * get_indentation()

    with LiveLine(console) as line:
        for req in iterable:
            if console.interactive and limiter.ready():
                line.update(
                    f"{indent}{current}/{total} [{req.name}] {_bar(current, total)}"
                )
                limiter.reset()
            yield req
            current += 1


def _raw_progress_bar(
    iterable: Iterable[bytes],
    *,
    size: int | None,
    initial_progress: int | None = None,
) -> Generator[bytes, None, None]:
    def write_progress(current: int, total: int) -> None:
        sys.stdout.write(f"Progress {current} of {total}\n")
        sys.stdout.flush()

    current = initial_progress or 0
    total = size or 0
    rate_limiter = RateLimiter(0.25)

    write_progress(current, total)
    for chunk in iterable:
        current += len(chunk)
        if rate_limiter.ready() or current == total:
            write_progress(current, total)
            rate_limiter.reset()
        yield chunk


def get_download_progress_renderer(
    *, bar_type: BarType, size: int | None = None, initial_progress: int | None = None
) -> ProgressRenderer[bytes]:
    """Get an object that can be used to render the download progress.

    Returns a callable, that takes an iterable to "wrap".
    """
    if bar_type == "on":
        return functools.partial(
            _download_progress_bar,
            size=size,
            initial_progress=initial_progress,
        )
    elif bar_type == "raw":
        return functools.partial(
            _raw_progress_bar,
            size=size,
            initial_progress=initial_progress,
        )
    else:
        return lambda iterable: iter(iterable)


def get_install_progress_renderer(
    *, bar_type: BarType, total: int
) -> ProgressRenderer[InstallRequirement]:
    """Get an object that can be used to render the install progress.
    Returns a callable, that takes an iterable to "wrap".
    """
    if bar_type == "on":
        return functools.partial(_install_progress_bar, total=total)
    else:
        return lambda iterable: iter(iterable)
