"""Text styling and serialized terminal output used by pip."""

from __future__ import annotations

import builtins
import json
import os
import re
import reprlib
import shutil
import sys
import threading
import traceback
import unicodedata
from typing import TYPE_CHECKING, Protocol, TextIO, runtime_checkable

from pip._internal.utils.compat import override

if TYPE_CHECKING:
    from types import TracebackType

    from typing_extensions import Self

if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    def _enable_windows_vt(file: TextIO) -> bool:
        """Enable ANSI output when the Windows console supports it."""
        try:
            handle = msvcrt.get_osfhandle(file.fileno())
        except (OSError, ValueError):
            return False
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetConsoleMode.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel.GetConsoleMode.restype = wintypes.BOOL
        kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.SetConsoleMode.restype = wintypes.BOOL
        mode = wintypes.DWORD()
        # Preserve existing flags while enabling ENABLE_VIRTUAL_TERMINAL_PROCESSING.
        # https://learn.microsoft.com/en-us/windows/console/setconsolemode
        return bool(
            kernel.GetConsoleMode(handle, ctypes.byref(mode))
            and kernel.SetConsoleMode(handle, mode.value | 0x0004)
        )

else:

    def _enable_windows_vt(_file: TextIO) -> bool:
        return False


_SGR_FOREGROUND = 30
# Use the basic SGR palette rather than terminal-specific RGB color support.
# https://learn.microsoft.com/en-us/windows/console/console-virtual-terminal-sequences#text-formatting
_STYLES = {
    "bold": "1",
    "dim": "2",
    "underline": "4",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "optparse.shortargs": "32",
    "optparse.longargs": "36",
    "optparse.groups": "1;34",
    "optparse.metavar": "33",
}
_JSON_TOKEN = re.compile(
    r'"|(?<!\w)(true|false|null|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b'
)
_TAG = re.compile(r"\\([\\\[])|\[([^\[\]\n]*)\]")
_STRIP_CONTROL = str.maketrans("", "", "\a\b\v\f\r")


def escape(text: str) -> str:
    """Quote brackets in text inserted into a styled message."""
    # Literal bracket escaping: https://rich.readthedocs.io/en/stable/markup.html#escaping
    return text.replace("\\", "\\\\").replace("[", r"\[")


class Text:
    """Literal text with the small set of styles used in pip diagnostics."""

    def __init__(self, text: str = "", style: str = "") -> None:
        self.parts = [(text, style)]

    @classmethod
    def from_markup(cls, text: str) -> Text:
        """Parse pip's finite style vocabulary, keeping unknown brackets literal."""
        result = cls()
        styles: list[str] = []
        start = 0

        for match in _TAG.finditer(text):
            escaped, tag = match.groups()
            result.parts.append((text[start : match.start()], " ".join(styles)))
            start = match.end()
            # Quote backslashes and brackets independently of surrounding markup.
            if escaped is not None:
                result.parts.append((escaped, " ".join(styles)))
            elif tag == "reset":
                styles.clear()
            elif tag == "/":
                if styles:
                    styles.pop()
            elif tag and all(word in _STYLES for word in tag.split()):
                styles.append(tag)
            else:
                result.parts.append((match[0], " ".join(styles)))

        result.parts.append((text[start:], " ".join(styles)))
        return result

    @classmethod
    def assemble(cls, *parts: str | Text | tuple[str, str]) -> Text:
        result = cls()

        for part in parts:
            if isinstance(part, tuple):
                result.parts.append(part)
            elif isinstance(part, Text):
                result.parts.extend(part.parts)
            else:
                result.parts.append((part, ""))

        return result

    def append(self, text: str) -> None:
        self.parts.append((text, ""))

    def __add__(self, other: Text) -> Text:
        return self.assemble(self, other)

    @override
    def __str__(self) -> str:
        return "".join(text for text, _ in self.parts)

    @override
    def __repr__(self) -> str:
        return f"Text({str(self)!r})"

    def format(self, terminal: Terminal) -> str:
        # Prefixing a diagnostic line must not reset the next line's style.
        return "".join(
            (
                text
                if not style or not terminal.styled
                else "\n".join(terminal.style(line, style) for line in text.split("\n"))
            )
            for text, style in self.parts
        )


@runtime_checkable
class Renderable(Protocol):
    def format(self, terminal: Terminal) -> str: ...


def _write(file: TextIO, text: str) -> None:
    """Write text with the stream's encoding and escape unrepresentable characters."""
    encoding = file.encoding or "utf-8"
    file.write(text.encode(encoding, "backslashreplace").decode(encoding))


class Display:
    """Coordinate streams that share a terminal cursor."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.lines: list[LiveLine] = []
        self.drawn: Terminal | None = None

    def clear(self) -> None:
        """Clear the active line under the caller's display lock."""
        if self.drawn is not None:
            _write(self.drawn.file, "\r\x1b[K")
            self.drawn.file.flush()
            self.drawn = None

    def paint(self) -> None:
        """Paint the innermost line under the caller's display lock."""
        if not self.lines:
            return

        line = self.lines[-1]
        owner = line.terminal
        if not owner.interactive:
            return

        width = max(shutil.get_terminal_size().columns - 1, 0)
        encoding = owner.file.encoding or "utf-8"
        text = line.text.encode(encoding, "backslashreplace").decode(encoding)
        # Leave the last column empty so a repaint never wraps.
        _write(owner.file, "\r" + fit_line(text, width))
        owner.file.flush()
        self.drawn = owner


class Terminal:
    """Own writes and a stack of progress lines for one output stream."""

    def __init__(
        self,
        file: TextIO | None = None,
        *,
        no_color: bool = False,
        color: bool | None = None,
    ) -> None:
        self.file = file if file is not None else sys.stdout
        tty = self.file.isatty()

        # Color-capable CI streams need not support cursor animation.
        # https://rich.readthedocs.io/en/stable/console.html#environment-variables
        compatible = os.environ.get("TTY_COMPATIBLE")
        capable = os.environ.get("TERM", "").lower() not in {"dumb", "unknown"}
        capable = capable and compatible != "0"
        if os.name == "nt" and tty:
            capable = capable and _enable_windows_vt(self.file)
        # An empty FORCE_COLOR explicitly disables automatic styling.
        forced = os.environ.get("FORCE_COLOR")
        self.styled = (
            color
            if color is not None
            else (compatible == "1" or (tty if forced is None else bool(forced)))
            and capable
        )
        self.color = (
            self.styled and not no_color and os.environ.get("NO_COLOR", "") == ""
        )
        interactive = os.environ.get("TTY_INTERACTIVE")
        self.interactive = capable and (
            interactive == "1" or (tty and interactive != "0")
        )

        self.ascii_only = False
        try:
            "\N{MULTIPLICATION SIGN}│╰─>".encode(self.file.encoding or "utf-8")
        except UnicodeEncodeError:
            self.ascii_only = True

        self.display = Display()

    def style(self, text: str, style: str) -> str:
        if not text or not style or not self.styled:
            return text

        codes: set[int] = set()
        foreground = None
        for word in style.split():
            for code in map(int, _STYLES[word].split(";")):
                if code >= _SGR_FOREGROUND:
                    foreground = code
                else:
                    codes.add(code)

        # A nested foreground replaces the enclosing color.
        if self.color and foreground is not None:
            codes.add(foreground)
        if not codes:
            return text

        return f"\x1b[{';'.join(map(str, sorted(codes)))}m{text}\x1b[0m"

    def format(self, value: str | Renderable) -> str:
        if isinstance(value, str):
            value = Text.from_markup(value)

        return value.format(self)

    def share_display(self, other: Terminal) -> None:
        """Share live-line state when both streams point at the same terminal."""
        if self.file is other.file:
            self.display = other.display
        elif self.interactive and other.interactive:
            try:
                same = os.fstat(self.file.fileno()) == os.fstat(other.file.fileno())
            except (OSError, ValueError):
                same = False
            if same:
                self.display = other.display

    def print(self, value: str | Renderable = "", *, indent: int = 0) -> None:
        text = self.format(value).translate(_STRIP_CONTROL)
        if indent:
            text = "\n".join(" " * indent + line for line in text.split("\n"))

        # Clear, write and repaint as one operation across shared streams.
        # Prior art: https://tqdm.github.io/docs/tqdm/#external_write_mode
        with self.display.lock:
            self.display.clear()
            _write(self.file, text + "\n")
            self.file.flush()
            self.display.paint()


class LiveLine:
    """Temporarily own the active progress line, restoring an outer line on exit."""

    def __init__(self, terminal: Terminal) -> None:
        self.terminal = terminal
        self.text = ""

    def __enter__(self) -> Self:
        with self.terminal.display.lock:
            self.terminal.display.clear()
            self.terminal.display.lines.append(self)
        return self

    def update(self, text: str) -> None:
        with self.terminal.display.lock:
            self.text = text
            if self.terminal.display.lines[-1] is self:
                self.terminal.display.clear()
                self.terminal.display.paint()

    def __exit__(self, *exc: object) -> None:
        with self.terminal.display.lock:
            self.terminal.display.clear()
            self.terminal.display.lines.remove(self)
            self.terminal.display.paint()


def fit_line(text: str, width: int) -> str:
    """Keep a progress line inside the terminal without embedded controls."""
    # Approximate terminal cells using Unicode combining and width properties.
    # https://docs.python.org/3/library/unicodedata.html#unicodedata.east_asian_width
    result = []
    for char in text:
        if not char.isprintable():
            continue
        cells = (
            0
            if unicodedata.combining(char)
            else (2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1)
        )
        if cells > width:
            break
        result.append(char)
        width -= cells

    return "".join(result)


def print_json(data: object, terminal: Terminal) -> None:
    """Write indented JSON, with token colors on color-enabled streams."""
    text = json.dumps(data, indent=2, ensure_ascii=False)
    # Text-stream backslash replacement can produce invalid JSON escapes.
    try:
        text.encode(terminal.file.encoding or "utf-8")
    except UnicodeEncodeError:
        text = json.dumps(data, indent=2, ensure_ascii=True)

    if not terminal.styled:
        terminal.print(Text(text))
        return

    decoder = json.JSONDecoder()
    # JSON tokens have no literal LF; each role can share one wrapper per document.
    # https://www.rfc-editor.org/rfc/rfc8259#section-7
    styles = {
        style: terminal.style("%s", style) for style in ("bold blue", "green", "cyan")
    }
    parts: list[str] = []
    start = 0
    while match := _JSON_TOKEN.search(text, start):
        parts.append(text[start : match.start()])
        if match[0] == '"':
            # The decoder skips escaped strings without regex backtracking state.
            # https://docs.python.org/3/library/json.html#json.JSONDecoder.raw_decode
            end = decoder.raw_decode(text, match.start())[1]
            style = "bold blue" if text[end : end + 1] == ":" else "green"
        else:
            end = match.end()
            style = "cyan"
        parts.append(styles[style] % text[match.start() : end])
        start = end

    parts.append(text[start:])
    text = "".join(parts)
    # Drop token fragments before the stream's encoding copies.
    del parts
    terminal.print(Text(text))


def _traceback_locals(
    rendered: traceback.TracebackException,
    exc: BaseException,
    tb: TracebackType | None,
) -> None:
    """Attach abbreviated locals to the native exception chain and group summaries."""
    # Retain native exception formatting while replacing only captured locals.
    # https://docs.python.org/3/library/traceback.html#traceback.FrameSummary
    pending = [(rendered, exc, tb)]
    while pending:
        summary, original, original_tb = pending.pop()
        for frame_summary, (frame, _) in zip(
            summary.stack, traceback.walk_tb(original_tb), strict=False
        ):
            # Abbreviate builtin strings and containers before taking their repr.
            # https://docs.python.org/3/library/reprlib.html#reprlib.repr
            frame_summary.locals = {
                name: reprlib.repr(value) for name, value in frame.f_locals.items()
            }

        if summary.__cause__ is not None and original.__cause__ is not None:
            cause = original.__cause__
            pending.append((summary.__cause__, cause, cause.__traceback__))
        if summary.__context__ is not None and original.__context__ is not None:
            context = original.__context__
            pending.append((summary.__context__, context, context.__traceback__))

        if (
            sys.version_info >= (3, 11)
            and isinstance(original, builtins.BaseExceptionGroup)
            and summary.exceptions is not None
        ):
            pending.extend(
                (child_summary, child, child.__traceback__)
                for child_summary, child in zip(
                    summary.exceptions, original.exceptions, strict=True
                )
            )


def debug_traceback(
    exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
) -> None:
    """Show exception chains and abbreviated local variables in debug mode."""
    rendered = traceback.TracebackException(exc_type, exc, tb, capture_locals=False)
    try:
        _traceback_locals(rendered, exc, tb)
    except Exception:
        # Unusual local representations must not hide the original exception.
        rendered = traceback.TracebackException(exc_type, exc, tb, capture_locals=False)

    sys.stderr.write("".join(rendered.format()))
