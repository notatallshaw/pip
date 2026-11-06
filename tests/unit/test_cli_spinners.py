from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager, nullcontext
from io import StringIO
from pathlib import Path
from unittest.mock import Mock

import pytest

from pip._internal.cli import spinners
from pip._internal.cli.spinners import open_auto_spinner
from pip._internal.cli.terminal import Terminal
from pip._internal.utils.subprocess import call_subprocess


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


def test_auto_spinner_runs_when_threads_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_start(_thread: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", reject_start)
    stream = StringIO()
    console = Terminal(stream, color=False)
    console.interactive = True

    with patch_logger_level(logging.INFO):
        with open_auto_spinner("working", console):
            console.print("operation ran")

    assert "operation ran\n" in stream.getvalue()
    assert stream.getvalue().endswith("working ... done\n")
    assert not console.display.lines


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


@pytest.mark.parametrize("draw_seconds", [0.0, 0.04, 0.3])
def test_auto_spinner_frame_deadlines(
    monkeypatch: pytest.MonkeyPatch, draw_seconds: float
) -> None:
    """Run the worker against a clock that charges for each terminal repaint."""
    now = 0.0
    frames: list[tuple[float, str]] = []
    waits: list[float] = []

    class TimedStream(StringIO):
        def write(self, text: str) -> int:
            nonlocal now
            if text.startswith("\rworking"):
                frames.append((now, text))
                now += draw_seconds
            return super().write(text)

    def wait(_event: threading.Event, timeout: float | None = None) -> bool:
        nonlocal now
        assert timeout is not None
        if len(waits) == 4:
            return True
        waits.append(timeout)
        now += timeout
        return False

    # Execute the real worker inline; only its clock and thread boundary are fake.
    monkeypatch.setattr(spinners.time, "monotonic", lambda: now)
    monkeypatch.setattr(threading.Event, "wait", wait)
    monkeypatch.setattr(threading.Thread, "start", lambda thread: thread.run())
    monkeypatch.setattr(threading.Thread, "join", lambda thread: None)
    stream = TimedStream()
    console = Terminal(stream, color=False)
    console.interactive = True

    with patch_logger_level(logging.INFO), open_auto_spinner("working", console):
        pass

    assert [frame for _, frame in frames] == [
        "\rworking ... -",
        "\rworking ... \\",
        "\rworking ... |",
        "\rworking ... /",
        "\rworking ... -",
    ]
    if draw_seconds < 1 / spinners.SPINS_PER_SECOND:
        assert [when for when, _ in frames] == pytest.approx(
            [0, 0.125, 0.25, 0.375, 0.5]
        )
    else:
        # An overloaded stream must not cause a burst of catch-up frames.
        assert waits[1:] == pytest.approx([0.125] * 3)


def test_subprocess_spinner_animates_without_child_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A silent child waits for a frame from the production spinner worker."""
    signal = tmp_path / "painted"
    caller = threading.current_thread()

    class ObservedStream(StringIO):
        def write(self, text: str) -> int:
            if threading.current_thread() is not caller:
                signal.touch()
            return super().write(text)

    stream = ObservedStream()
    console = Terminal(stream, color=False)
    console.interactive = True
    monkeypatch.setattr(spinners, "get_console", lambda: console)
    command = [
        sys.executable,
        "-c",
        "import pathlib, sys, time\n"
        "signal = pathlib.Path(sys.argv[1])\n"
        "deadline = time.monotonic() + 5\n"
        "while not signal.exists():\n"
        "    if time.monotonic() > deadline:\n"
        "        sys.exit('spinner did not animate')\n"
        "    time.sleep(0.01)\n",
        str(signal),
    ]
    with patch_logger_level(logging.INFO), spinners.open_spinner("working") as spinner:
        call_subprocess(command, command_desc="silent backend", spinner=spinner)

    assert signal.exists()
    assert stream.getvalue().endswith("working ... done\n")
    assert not console.display.lines
    assert not any(thread.name == "pip-spinner" for thread in threading.enumerate())


@pytest.mark.parametrize("exception", [None, ValueError, KeyboardInterrupt, SystemExit])
def test_nested_spinners_restore_outer_display(
    monkeypatch: pytest.MonkeyPatch, exception: type[BaseException] | None
) -> None:
    """Only the inner worker paints until it releases the shared cursor."""
    caller = threading.current_thread()
    outer_painted = threading.Event()
    inner_painted = threading.Event()

    class ObservedStream(StringIO):
        def write(self, text: str) -> int:
            written = super().write(text)
            if threading.current_thread() is not caller:
                if text.startswith("\router"):
                    outer_painted.set()
                elif text.startswith("\rinner"):
                    inner_painted.set()
            return written

    stream = ObservedStream()
    console = Terminal(stream, color=False)
    console.interactive = True
    diagnostics = Terminal(stream, color=False)
    diagnostics.interactive = True
    diagnostics.share_display(console)
    monkeypatch.setattr(spinners, "get_console", lambda: console)

    with patch_logger_level(logging.INFO), open_auto_spinner("outer", console):
        assert outer_painted.wait(5)
        expected = pytest.raises(exception) if exception is not None else nullcontext()

        with expected, spinners.open_spinner("inner"):
            with console.display.lock:
                start = len(stream.getvalue())
            assert inner_painted.wait(5)
            diagnostics.print("notice")

            with console.display.lock:
                output = stream.getvalue()[start:]
                outer_painted.clear()
            assert "\rinner ... " in output
            assert "\router ... " not in output
            assert "notice\n\rinner ... " in output

            if exception is not None:
                raise exception

        assert outer_painted.wait(5), "outer spinner did not resume"
        assert (
            sum(thread.name == "pip-spinner" for thread in threading.enumerate()) == 1
        )

    assert stream.getvalue().endswith("outer ... done\n")
    assert not console.display.lines
    assert not any(thread.name == "pip-spinner" for thread in threading.enumerate())


def test_subprocess_spinner_callback_without_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thread exhaustion retains callback animation and ordinary completion."""

    def reject_start(_thread: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", reject_start)
    stream = StringIO()
    console = Terminal(stream, color=False)
    console.interactive = True
    now = 0.0
    monkeypatch.setattr(spinners.time, "monotonic", lambda: now)
    spinner = spinners.InteractiveSpinner("working", console=console)
    now = 0.125
    spinner.spin()
    spinner.finish("done")
    spinner.finish("done")
    spinner.spin()

    assert "\rworking ... \\" in stream.getvalue()
    assert stream.getvalue().count("working ... done\n") == 1
    assert not console.display.lines


def test_spinner_initial_write_failure_releases_line() -> None:
    class BrokenStream(StringIO):
        def write(self, text: str) -> int:
            raise BrokenPipeError

    console = Terminal(BrokenStream(), color=False)
    console.interactive = True
    with pytest.raises(BrokenPipeError):
        spinners.InteractiveSpinner("working", console=console)

    assert not console.display.lines
    assert not any(thread.name == "pip-spinner" for thread in threading.enumerate())


def test_subprocess_callbacks_do_not_advance_worker_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subprocess output must not add frames between the worker's deadlines."""
    monkeypatch.setattr(threading.Thread, "start", lambda thread: None)
    monkeypatch.setattr(threading.Thread, "join", lambda thread: None)
    monkeypatch.setattr(spinners.RateLimiter, "ready", lambda limiter: True)
    stream = StringIO()
    console = Terminal(stream, color=False)
    console.interactive = True
    spinner = spinners.InteractiveSpinner("working", console=console)
    initial = stream.getvalue()

    for _ in range(10):
        spinner.spin()
    assert stream.getvalue() == initial

    spinner.finish("done")
    assert stream.getvalue().endswith("working ... done\n")
