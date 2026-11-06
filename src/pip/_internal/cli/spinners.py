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
    """Animate an operation until finish() releases its live line."""

    def __init__(
        self,
        message: str,
        file: TextIO | None = None,
        spin_chars: str = SPINNER_CHARS,
        # Empirically, 8 updates/second looks nice
        min_update_interval_seconds: float = 1 / SPINS_PER_SECOND,
        *,
        console: Terminal | None = None,
    ):
        if console is None:
            console = (
                get_console() if file is None or file is sys.stdout else Terminal(file)
            )

        self._console = console
        self._line = LiveLine(self._console)
        self._rate_limiter = RateLimiter(min_update_interval_seconds)
        self._interval = min_update_interval_seconds
        self._finished = False
        self._spin_cycle = itertools.cycle(spin_chars)
        self._prefix = " " * get_indentation() + message + " ... "
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

        self._next_update = time.monotonic() + self._interval
        self._line.__enter__()
        try:
            self._line.update(self._prefix + next(self._spin_cycle))
        except BaseException:
            self._line.__exit__()
            raise

        self._rate_limiter.reset()

        # A silent backend blocks readline(), so animation needs its own clock.
        # Prior art: https://rich.readthedocs.io/en/stable/live.html#auto-refresh
        if self._console.interactive:
            thread = threading.Thread(
                target=self._repaint, name="pip-spinner", daemon=True
            )
            try:
                thread.start()
            except RuntimeError:
                # Keep callback updates when the OS cannot create a worker.
                pass
            else:
                self._thread = thread

    def _repaint(self) -> None:
        """Advance frames at monotonic deadlines without accumulating draw time."""
        # https://docs.python.org/3/library/time.html#time.monotonic
        deadline = self._next_update

        while not self._stopped.wait(max(deadline - time.monotonic(), 0)):
            try:
                self._line.update(self._prefix + next(self._spin_cycle))
            except OSError:
                # Leave the final output attempt to the calling thread.
                return

            deadline += self._interval
            now = time.monotonic()
            if deadline <= now:
                # A stalled terminal must not trigger a burst of overdue frames.
                deadline = now + self._interval

    @override
    def spin(self) -> None:
        if self._finished or self._thread is not None or not self._rate_limiter.ready():
            return

        self._line.update(self._prefix + next(self._spin_cycle))
        self._rate_limiter.reset()

    @override
    def finish(self, final_status: str) -> None:
        if self._finished:
            return

        # Stop the worker before releasing its line or printing the final status.
        # https://docs.python.org/3/library/threading.html#threading.Thread.join
        self._stopped.set()
        if self._thread is not None:
            self._thread.join()

        self._line.__exit__()
        # Do not release the line twice if the final status write fails.
        self._finished = True

        self._console.print(Text(self._prefix + final_status))


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

    spinner = InteractiveSpinner(label, console=console)
    status = "done"

    try:
        yield
    except KeyboardInterrupt:
        status = "canceled"
        raise
    except BaseException:
        status = "error"
        raise
    finally:
        spinner.finish(status)
