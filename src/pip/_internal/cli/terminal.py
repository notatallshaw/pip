"""Text styling and serialized terminal output used by pip."""

from __future__ import annotations

import json
import os
import re
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
        return bool(
            kernel.GetConsoleMode(handle, ctypes.byref(mode))
            and kernel.SetConsoleMode(handle, mode.value | 0x0004)
        )

else:

    def _enable_windows_vt(_file: TextIO) -> bool:
        return False


_SGR_FOREGROUND = 30
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
    r'("(?:[^"\\]|\\.)*")(\s*:)?|(?<!\w)'
    r"(true|false|null|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b"
)
_TAG = re.compile(r"(\\*)\[(?:([^\[\]\n]*)\])?")
_STRIP_CONTROL = str.maketrans("", "", "\a\b\v\f\r")


def escape(text: str) -> str:
    """Quote brackets in text inserted into a styled message."""
    return re.sub(r"(\\*)\[", r"\1\1\\[", text)


class Text:
    """Literal text with the small set of styles used in pip diagnostics."""

    def __init__(self, text: str = "", style: str = "") -> None:
        self.parts = [(text, style)]

    @classmethod
    def from_markup(cls, text: str) -> Text:
        result = cls()
        styles: list[str] = []
        start = 0
        for match in _TAG.finditer(text):
            slashes, tag = match.groups()
            result.parts.append(
                (
                    text[start : match.start()] + ("\\" * (len(slashes) // 2)),
                    " ".join(styles),
                )
            )
            start = match.end()
            if tag is None:
                result.parts.append(("[", " ".join(styles)))
            elif len(slashes) % 2:
                result.parts.append((f"[{tag}]", " ".join(styles)))
            elif tag == "reset":
                styles.clear()
            elif tag == "/":
                if styles:
                    styles.pop()
            elif tag and all(word in _STYLES for word in tag.split()):
                styles.append(tag)
            else:
                result.parts.append((match[0][len(slashes) :], " ".join(styles)))
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
            "\n".join(terminal.style(line, style) for line in text.split("\n"))
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
        capable = os.environ.get("TERM", "") not in {"dumb", "unknown"}
        if os.name == "nt" and tty:
            capable = capable and _enable_windows_vt(self.file)
        self.styled = (
            color
            if color is not None
            else (tty or "FORCE_COLOR" in os.environ) and capable
        )
        self.color = self.styled and not no_color and "NO_COLOR" not in os.environ
        self.interactive = tty and capable
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
    parts: list[str | tuple[str, str]] = []
    start = 0
    for match in _JSON_TOKEN.finditer(text):
        parts.append(text[start : match.start()])
        if match[1] is not None:
            style = "bold blue" if match[2] is not None else "green"
            parts.extend([(match[1], style), match[2] or ""])
        else:
            parts.append((match[0], "cyan"))
        start = match.end()
    parts.append(text[start:])
    terminal.print(Text.assemble(*parts))


def debug_traceback(
    exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
) -> None:
    """Show exception chains and local variables in debug mode."""
    try:
        rendered = traceback.TracebackException(exc_type, exc, tb, capture_locals=True)
    except Exception:
        # A local's repr() can raise any exception on older Python versions.
        rendered = traceback.TracebackException(exc_type, exc, tb, capture_locals=False)
    sys.stderr.write("".join(rendered.format()))
