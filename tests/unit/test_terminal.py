from __future__ import annotations

import builtins
import io
import json
import os
import re
import select
import sys

import pytest

from pip._internal.cli import terminal
from pip._internal.cli.terminal import (
    LiveLine,
    Terminal,
    Text,
    debug_traceback,
    escape,
    fit_line,
    print_json,
)


class TtyStream(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def terminal_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "NO_COLOR",
        "FORCE_COLOR",
        "TERM",
        "TTY_COMPATIBLE",
        "TTY_INTERACTIVE",
    ):
        monkeypatch.delenv(name, raising=False)
    # The in-memory stream has no Windows console handle.
    monkeypatch.setattr(terminal, "_enable_windows_vt", lambda file: True)


@pytest.mark.parametrize(
    "value",
    [
        "[red]literal[/]",
        r"C:\[build-system]",
        "open[",
        r"\\[red]",
        "[]",
        "C:\\",
        r"\\server\share",
    ],
)
def test_literal_text(value: str) -> None:
    output = Terminal(color=True)
    assert output.format(Text(value)) == value
    assert output.format(escape(value)) == value


@pytest.mark.parametrize("color", [False, True])
@pytest.mark.parametrize(
    "closing, suffix",
    [("[/]", " next"), ("[reset]", " next"), ("", ""), ("", " ordinary text")],
)
@pytest.mark.parametrize("backslashes", [1, 2, 3])
def test_escaped_trailing_backslashes(
    color: bool, closing: str, suffix: str, backslashes: int
) -> None:
    value = "failed at path" + "\\" * backslashes
    rendered = Terminal(color=color).format(
        "[green]" + escape(value) + closing + suffix
    )
    assert re.sub(r"\x1b\[[0-9;]*m", "", rendered) == value + suffix


def test_nested_style() -> None:
    stream = io.StringIO()
    Terminal(stream, color=True).print("[bold]a[red]b[/]c[/]")
    assert stream.getvalue() == "\x1b[1ma\x1b[0m\x1b[1;31mb\x1b[0m\x1b[1mc\x1b[0m\n"


@pytest.mark.parametrize(
    "tty, environment, no_color, color, interactive",
    [
        (False, {}, False, False, False),
        (True, {}, False, True, True),
        (False, {"FORCE_COLOR": "1"}, False, True, False),
        (False, {"FORCE_COLOR": ""}, False, False, False),
        (True, {"FORCE_COLOR": ""}, False, False, True),
        (True, {"TTY_COMPATIBLE": "0", "FORCE_COLOR": "1"}, False, False, False),
        (True, {"TTY_INTERACTIVE": "0"}, False, True, False),
        (True, {"NO_COLOR": "1"}, False, False, True),
        (True, {"FORCE_COLOR": "1"}, True, False, True),
        (True, {"TERM": "dumb"}, False, False, False),
        (True, {"TERM": "DUMB", "FORCE_COLOR": "1"}, False, False, False),
    ],
)
def test_color_and_animation(
    monkeypatch: pytest.MonkeyPatch,
    tty: bool,
    environment: dict[str, str],
    no_color: bool,
    color: bool,
    interactive: bool,
) -> None:
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    output = Terminal(TtyStream() if tty else io.StringIO(), no_color=no_color)
    assert output.color is color
    assert output.interactive is interactive


def test_nested_progress_and_logging() -> None:
    stream = TtyStream()
    output = Terminal(stream, color=False)
    diagnostics = Terminal(stream, color=False)
    diagnostics.share_display(output)
    with LiveLine(output) as outer:
        outer.update("outer")
        with LiveLine(output) as inner:
            inner.update("inner")
            diagnostics.print(Text("diagnostic"))
        output.print(Text("after inner"))
    output.print(Text("finished"))
    assert stream.getvalue() == (
        "\router\r\x1b[K\rinner\r\x1b[Kdiagnostic\n\rinner"
        "\r\x1b[K\router\r\x1b[Kafter inner\n\router"
        "\r\x1b[Kfinished\n"
    )


def test_progress_cleanup_on_failure() -> None:
    stream = TtyStream()
    output = Terminal(stream, color=False)

    def operation() -> None:
        with LiveLine(output) as line:
            line.update("running")
            raise RuntimeError("operation failed")

    with pytest.raises(RuntimeError, match="operation failed"):
        operation()
    output.print(Text("next operation"))
    assert stream.getvalue().endswith("\r\x1b[Knext operation\n")


@pytest.mark.parametrize(
    "text, width, expected",
    [
        ("abcdefgh", 3, "abc"),
        ("你好abc", 5, "你好a"),
        ("e\u0301x", 1, "e\u0301"),
        ("a\n\rb", 3, "ab"),
        ("abc", 0, ""),
    ],
)
def test_progress_line_width(text: str, width: int, expected: str) -> None:
    assert fit_line(text, width) == expected


def test_ascii_output_preserves_unicode_data() -> None:
    data = io.BytesIO()
    stream = io.TextIOWrapper(data, encoding="ascii", newline="")
    output = Terminal(stream, color=False)
    output.print(Text("你好"))
    assert data.getvalue() == b"\\u4f60\\u597d\n"
    stream.detach()


@pytest.mark.parametrize("color", [False, True])
def test_json_roundtrip(color: bool) -> None:
    data = {"[red]": [True, None, -2.5, 'a"b', "你好", '\\\\"\\u1234 true -42']}
    stream = io.StringIO()
    print_json(data, Terminal(stream, color=color))
    text = stream.getvalue()
    assert ("\x1b[" in text) is color
    assert json.loads(re.sub(r"\x1b\[[0-9;]*m", "", text)) == data


def test_debug_traceback_locals(capsys: pytest.CaptureFixture[str]) -> None:
    local_value = "visible-local"
    try:
        raise ValueError(local_value)
    except ValueError as exc:
        debug_traceback(type(exc), exc, exc.__traceback__)
    text = capsys.readouterr().err
    assert "local_value = 'visible-local'" in text
    assert "ValueError: visible-local" in text


def test_no_color_retains_emphasis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = Terminal(TtyStream())
    assert output.format(Text("heading", "bold blue")) == "\x1b[1mheading\x1b[0m"


@pytest.mark.skipif(sys.platform == "win32", reason="requires a POSIX pseudo-terminal")
def test_stdout_and_stderr_share_cursor() -> None:
    master, slave = os.openpty()
    try:
        with (
            os.fdopen(os.dup(slave), "w", encoding="utf-8") as stdout,
            os.fdopen(os.dup(slave), "w", encoding="utf-8") as stderr,
        ):
            output = Terminal(stdout, color=False)
            diagnostics = Terminal(stderr, color=False)
            diagnostics.share_display(output)
            with LiveLine(output) as line:
                line.update("downloading")
                diagnostics.print(Text("warning"))
            expected = b"\rdownloading\r\x1b[Kwarning\r\n\rdownloading\r\x1b[K"
            received = bytearray()
            while len(received) < len(expected):
                ready = select.select([master], [], [], 5)[0]
                assert ready, "terminal output was incomplete"
                received.extend(os.read(master, 4096))
        assert received == expected
    finally:
        os.close(slave)
        os.close(master)


def test_nested_foreground_overrides_outer_color() -> None:
    output = Terminal(color=True)
    assert output.format("[blue]a[red]b[/]c[/]") == (
        "\x1b[34ma\x1b[0m\x1b[31mb\x1b[0m\x1b[34mc\x1b[0m"
    )


def test_multiline_text_restores_style_after_line_prefix() -> None:
    output = Terminal(color=True)
    text = output.format(Text("first\nsecond", "green"))
    assert text.split("\n") == ["\x1b[32mfirst\x1b[0m", "\x1b[32msecond\x1b[0m"]


def test_json_styles_keys_and_complete_negative_numbers() -> None:
    stream = io.StringIO()
    print_json({"value": -2.5}, Terminal(stream, color=True))
    assert '\x1b[1;34m"value"\x1b[0m:' in stream.getvalue()
    assert "\x1b[36m-2.5\x1b[0m" in stream.getvalue()


def test_debug_traceback_preserves_exception_when_local_repr_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class BrokenRepr:
        def __repr__(self) -> str:
            raise RuntimeError("repr failed")

    def crash(value: BrokenRepr) -> None:
        raise ValueError("original failure")

    try:
        crash(BrokenRepr())
    except ValueError as exc:
        debug_traceback(type(exc), exc, exc.__traceback__)
    assert "ValueError: original failure" in capsys.readouterr().err


def test_debug_traceback_abbreviates_locals(capsys: pytest.CaptureFixture[str]) -> None:
    def crash() -> None:
        description = "x" * 100_000
        dependencies = ["example>=1"] * 10_000
        assert description
        assert dependencies
        raise ValueError("original failure")

    try:
        crash()
    except ValueError as exc:
        debug_traceback(type(exc), exc, exc.__traceback__)

    text = capsys.readouterr().err
    assert "ValueError: original failure" in text
    assert "description = " in text
    assert "dependencies = " in text
    assert "x" * 100 not in text
    assert text.count("'example>=1'") < 20


@pytest.mark.parametrize("explicit_cause", [False, True])
def test_debug_traceback_chain_locals(
    explicit_cause: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    def crash() -> None:
        inner_local = "inner-visible"
        raise ValueError(inner_local)

    try:
        try:
            crash()
        except ValueError as cause:
            if explicit_cause:
                raise RuntimeError("outer failure") from cause
            raise RuntimeError("outer failure")
    except RuntimeError as exc:
        debug_traceback(type(exc), exc, exc.__traceback__)

    text = capsys.readouterr().err
    assert "inner_local = 'inner-visible'" in text
    assert "ValueError: inner-visible" in text
    assert "RuntimeError: outer failure" in text


def test_debug_traceback_group_locals(capsys: pytest.CaptureFixture[str]) -> None:
    if sys.version_info < (3, 11):
        pytest.skip("exception groups require Python 3.11")

    def child_exception() -> ValueError:
        child_local = "child-visible"
        try:
            raise ValueError(child_local)
        except ValueError as exc:
            return exc

    child = child_exception()
    try:
        raise builtins.ExceptionGroup("group failure", [child])
    except builtins.ExceptionGroup as exc:
        debug_traceback(type(exc), exc, exc.__traceback__)

    text = capsys.readouterr().err
    assert "ExceptionGroup: group failure" in text
    assert "child_local = 'child-visible'" in text
    assert "ValueError: child-visible" in text


def test_print_strips_text_controls_but_keeps_newlines() -> None:
    stream = io.StringIO()
    Terminal(stream, color=False).print(Text("hello\a\b\v\f\r\nworld"))
    assert stream.getvalue() == "hello\nworld\n"


def test_windows_dumb_terminal_stays_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "dumb")
    with monkeypatch.context() as platform:
        platform.setattr(terminal.os, "name", "nt")
        output = Terminal(TtyStream())
    assert not output.color
    assert not output.interactive


def test_json_ascii_stream_preserves_non_bmp_text() -> None:
    data = {"summary": "launch 🚀"}
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="ascii", newline="")
    print_json(data, Terminal(stream, color=False))
    assert json.loads(buffer.getvalue().decode("ascii")) == data
    stream.detach()
