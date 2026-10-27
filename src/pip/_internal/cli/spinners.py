from __future__ import annotations

import contextlib
import itertools
import logging
import sys
import threading
import time
from collections.abc import Generator
from typing import Final, TextIO

from pip._internal.cli.terminal import LiveLine, Terminal, Text
from pip._internal.utils.compat import override
from pip._internal.utils.logging import get_console, get_indentation

logger = logging.getLogger(__name__)

SPINNER_CHARS: Final = r"-\|/"
SPINS_PER_SECOND: Final = 8


class SpinnerInterface:
    def spin(self) -> None:
        raise NotImplementedError()

    def finish(self, final_status: str) -> None:
        raise NotImplementedError()


class InteractiveSpinner(SpinnerInterface):
    def __init__(
        self,
        message: str,
        file: TextIO | None = None,
        spin_chars: str = SPINNER_CHARS,
        # Empirically, 8 updates/second looks nice
        min_update_interval_seconds: float = 1 / SPINS_PER_SECOND,
    ):
        self._message = message
        self._console = (
            get_console() if file is None or file is sys.stdout else Terminal(file)
        )
        self._line = LiveLine(self._console)
        self._line.__enter__()
        self._rate_limiter = RateLimiter(min_update_interval_seconds)
        self._finished = False
        self._spin_cycle = itertools.cycle(spin_chars)
        self._prefix = " " * get_indentation() + self._message + " ... "
        self._line.update(self._prefix + next(self._spin_cycle))

    @override
    def spin(self) -> None:
        if self._finished or not self._rate_limiter.ready():
            return
        self._line.update(self._prefix + next(self._spin_cycle))
        self._rate_limiter.reset()

    @override
    def finish(self, final_status: str) -> None:
        if self._finished:
            return
        self._line.__exit__()
        self._console.print(Text(self._prefix + final_status))
        self._finished = True


# Used for dumb terminals, non-interactive installs (no tty), etc.
# We still print updates occasionally (once every 60 seconds by default) to
# act as a keep-alive for systems like Travis-CI that take lack-of-output as
# an indication that a task has frozen.
class NonInteractiveSpinner(SpinnerInterface):
    def __init__(self, message: str, min_update_interval_seconds: float = 60.0) -> None:
        self._message = message
        self._finished = False
        self._rate_limiter = RateLimiter(min_update_interval_seconds)
        self._update("started")

    def _update(self, status: str) -> None:
        assert not self._finished
        self._rate_limiter.reset()
        logger.info("%s: %s", self._message, status)

    @override
    def spin(self) -> None:
        if self._finished:
            return
        if not self._rate_limiter.ready():
            return
        self._update("still running...")

    @override
    def finish(self, final_status: str) -> None:
        if self._finished:
            return
        self._update(f"finished with status '{final_status}'")
        self._finished = True


class RateLimiter:
    def __init__(self, min_update_interval_seconds: float) -> None:
        self._min_update_interval_seconds = min_update_interval_seconds
        self._last_update: float = 0

    def ready(self) -> bool:
        now = time.monotonic()
        delta = now - self._last_update
        return delta >= self._min_update_interval_seconds

    def reset(self) -> None:
        self._last_update = time.monotonic()


@contextlib.contextmanager
def open_spinner(message: str) -> Generator[SpinnerInterface, None, None]:
    # Interactive spinner goes directly to sys.stdout rather than being routed
    # through the logging system, but it acts like it has level INFO,
    # i.e. it's only displayed if we're at level INFO or better.
    # Non-interactive spinner goes through the logging system, so it is always
    # in sync with logging configuration.
    if get_console().interactive and logger.getEffectiveLevel() <= logging.INFO:
        spinner: SpinnerInterface = InteractiveSpinner(message)
    else:
        spinner = NonInteractiveSpinner(message)
    try:
        yield spinner
    except KeyboardInterrupt:
        spinner.finish("canceled")
        raise
    except BaseException:
        spinner.finish("error")
        raise
    else:
        spinner.finish("done")


@contextlib.contextmanager
def open_auto_spinner(label: str, console: Terminal | None = None) -> Generator[None]:
    """Animate while the calling thread waits for an in-process build."""
    if not logger.isEnabledFor(logging.INFO):
        yield
        return

    if console is None:
        console = get_console()

    prefix = " " * get_indentation() + label + " ... "
    stopped = threading.Event()
    status = "done"
    try:
        with LiveLine(console) as line:

            def repaint() -> None:
                for frame in itertools.cycle(SPINNER_CHARS):
                    if stopped.wait(1 / SPINS_PER_SECOND):
                        return
                    try:
                        line.update(prefix + frame)
                    except OSError:
                        # Report output failures on the calling thread at completion.
                        return

            thread = None
            if console.interactive:
                line.update(prefix + SPINNER_CHARS[0])
                thread = threading.Thread(
                    target=repaint, name="pip-spinner", daemon=True
                )
                thread.start()

            try:
                yield
            except KeyboardInterrupt:
                status = "canceled"
                raise
            except BaseException:
                status = "error"
                raise
            finally:
                # Stop the worker before releasing its live line.
                stopped.set()
                if thread is not None:
                    thread.join()
    finally:
        console.print(Text(prefix + status))
