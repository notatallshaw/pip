from __future__ import annotations

from collections.abc import Iterator
from io import StringIO

import pytest

from pip._internal.cli import progress_bars
from pip._internal.cli.progress_bars import (
    get_download_progress_renderer,
    get_install_progress_renderer,
)
from pip._internal.cli.terminal import Terminal
from pip._internal.req.constructors import install_req_from_line
from pip._internal.req.req_install import InstallRequirement


def test_resumed_download_counts_and_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = StringIO()
    output = Terminal(stream, color=False)
    monkeypatch.setattr(progress_bars, "get_console", lambda: output)
    now = 10.0
    monkeypatch.setattr(progress_bars.time, "monotonic", lambda: now)
    chunks = [b"a" * 2000, b"b" * 2000]

    def download() -> Iterator[bytes]:
        nonlocal now
        for chunk in chunks:
            now += 2
            yield chunk

    render = get_download_progress_renderer(
        bar_type="on", size=10000, initial_progress=6000
    )
    assert list(render(download())) == chunks
    text = stream.getvalue()
    assert "10.0 kB/10.0 kB" in text
    assert "1000 bytes/s" in text
    assert text.endswith("0:00:04\n")
    assert "\r" not in text
    assert "\x1b" not in text


def test_unknown_download_size(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = StringIO()
    monkeypatch.setattr(
        progress_bars, "get_console", lambda: Terminal(stream, color=False)
    )
    chunks = [b"a" * 2000]
    assert list(get_download_progress_renderer(bar_type="on")(chunks)) == chunks
    assert "2.0 kB" in stream.getvalue()
    assert "eta" not in stream.getvalue()
    assert "[=" not in stream.getvalue()


def test_raw_progress_preserves_offset(capsys: pytest.CaptureFixture[str]) -> None:
    chunks = [b"abc"]
    render = get_download_progress_renderer(bar_type="raw", size=10, initial_progress=7)
    assert list(render(chunks)) == chunks
    output = capsys.readouterr()
    assert output.out.startswith("Progress 7 of 10\n")
    assert output.out.endswith("Progress 10 of 10\n")
    assert not output.err


def test_install_nonterminal_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = StringIO()
    monkeypatch.setattr(
        progress_bars, "get_console", lambda: Terminal(stream, color=False)
    )
    requirements = [install_req_from_line("demo==1"), install_req_from_line("other==2")]
    assert (
        list(get_install_progress_renderer(bar_type="on", total=2)(requirements))
        == requirements
    )
    assert not stream.getvalue()


def test_download_failure_closes_line(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = StringIO()
    output = Terminal(stream, color=False)
    output.interactive = True
    monkeypatch.setattr(progress_bars, "get_console", lambda: output)

    def download() -> Iterator[bytes]:
        yield b"abc"
        raise OSError("transfer failed")

    with pytest.raises(OSError, match="transfer failed"):
        list(get_download_progress_renderer(bar_type="on", size=10)(download()))
    output.print("after failure")
    assert stream.getvalue().endswith("\r\x1b[Kafter failure\n")


def test_install_counts_completed_requirements(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = StringIO()
    output = Terminal(stream, color=False)
    output.interactive = True
    monkeypatch.setattr(progress_bars, "get_console", lambda: output)
    now = 10.0
    monkeypatch.setattr(progress_bars.time, "monotonic", lambda: now)
    requirements = [install_req_from_line("demo==1"), install_req_from_line("other==2")]

    def installs() -> Iterator[InstallRequirement]:
        nonlocal now
        for requirement in requirements:
            now += 1
            yield requirement

    rendered = get_install_progress_renderer(bar_type="on", total=2)(installs())
    assert list(rendered) == requirements
    text = stream.getvalue()
    assert "0/2 [demo]" in text
    assert "1/2 [other]" in text
    assert text.endswith("\r\x1b[K")


def test_narrow_download_shows_byte_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "16")
    stream = StringIO()
    output = Terminal(stream, color=False)
    output.interactive = True
    monkeypatch.setattr(progress_bars, "get_console", lambda: output)
    render = get_download_progress_renderer(bar_type="on", size=4000)
    assert list(render([b"a" * 2000, b"b" * 2000]))
    first_frame = stream.getvalue().split("\r")[1]
    assert first_frame == "  2.0 kB/4.0 kB"
