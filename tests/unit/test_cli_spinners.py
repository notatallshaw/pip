from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from io import StringIO
from unittest.mock import Mock

import pytest

from pip._internal.cli import spinners
from pip._internal.cli.spinners import open_auto_spinner
from pip._internal.cli.terminal import Terminal


@contextmanager
def patch_logger_level(level: int) -> Generator[None]:
    """Patch the spinner logger level temporarily."""
    original_level = spinners.logger.level
    spinners.logger.setLevel(level)
    try:
        yield
    finally:
        spinners.logger.setLevel(original_level)


class TestAutoSpinner:
    @pytest.mark.parametrize(
        "status, func",
        [
            ("done", lambda: None),
            ("error", lambda: 1 / 0),
            ("canceled", Mock(side_effect=KeyboardInterrupt)),
        ],
    )
    def test_finish(self, status: str, func: Callable[[], None]) -> None:
        """
        Check that the spinner finish message is set correctly depending
        on how the spinner came to a stop.
        """
        stream = StringIO()
        try:
            with patch_logger_level(logging.INFO):
                with open_auto_spinner("working", Terminal(file=stream)):
                    func()
        except BaseException:
            pass

        output = stream.getvalue()
        assert output == f"working ... {status}\n"

    @pytest.mark.parametrize(
        "level, visible",
        [(logging.ERROR, False), (logging.INFO, True), (logging.DEBUG, True)],
    )
    def test_verbosity(self, level: int, visible: bool) -> None:
        """Is the spinner hidden at the appropriate verbosity?"""
        stream = StringIO()
        with patch_logger_level(level):
            with open_auto_spinner("working", Terminal(file=stream)):
                pass

        assert bool(stream.getvalue()) == visible


def test_auto_spinner_animates_and_stops() -> None:
    painted = threading.Event()
    caller = threading.current_thread()

    class ObservedStream(StringIO):
        def write(self, text: str) -> int:
            written = super().write(text)
            if threading.current_thread() is not caller:
                painted.set()
            return written

    stream = ObservedStream()
    console = Terminal(file=stream, color=False)
    console.interactive = True
    with patch_logger_level(logging.INFO):
        with open_auto_spinner("working", console):
            assert painted.wait(5), "spinner did not repaint while the caller waited"
    assert stream.getvalue().endswith("working ... done\n")
    assert not any(thread.name == "pip-spinner" for thread in threading.enumerate())


@pytest.mark.parametrize("exception", [SystemExit, GeneratorExit])
def test_subprocess_spinner_cleans_up_base_exception(
    monkeypatch: pytest.MonkeyPatch, exception: type[BaseException]
) -> None:
    stream = StringIO()
    console = Terminal(stream, color=False)
    console.interactive = True
    monkeypatch.setattr(spinners, "get_console", lambda: console)
    with patch_logger_level(logging.INFO):
        with pytest.raises(exception), spinners.open_spinner("working"):
            raise exception
    console.print("after spinner")
    assert stream.getvalue().endswith("working ... error\nafter spinner\n")
