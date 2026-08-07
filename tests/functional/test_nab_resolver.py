"""End to end checks that pip's resolver is nab.

Each resolving command has to reach the engine and come back with an answer.
``NOT_IMPLEMENTED`` is still asserted absent: it is the sentence the seam
raised before the engine was wired, so a run that prints it has lost the
engine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.lib import PipTestEnvironment, TestData

NOT_IMPLEMENTED = "nab is not vendored into pip"


@pytest.mark.parametrize("command", ["download", "wheel", "install"])
def test_every_resolving_command_reaches_the_engine(
    script: PipTestEnvironment, data: TestData, command: str
) -> None:
    args: list[str] = [command, "--no-index", "-f", str(data.packages)]
    if command == "download":
        args += ["-d", "."]
    elif command == "wheel":
        args += ["-w", "."]
    result = script.pip(*args, "simplewheel")
    assert NOT_IMPLEMENTED not in result.stderr, str(result)
    assert "simplewheel" in result.stdout, str(result)


def test_lock_reaches_the_engine(script: PipTestEnvironment, data: TestData) -> None:
    result = script.pip(
        "lock",
        "--no-index",
        "-f",
        str(data.packages),
        "-o",
        "pylock.toml",
        "simplewheel",
        allow_stderr_warning=True,
    )
    assert NOT_IMPLEMENTED not in result.stderr, str(result)
    result.did_create(Path("scratch") / "pylock.toml")


def test_legacy_resolver_still_resolves(
    script: PipTestEnvironment, data: TestData
) -> None:
    result = script.pip(
        "download",
        "--no-index",
        "-f",
        str(data.packages),
        "-d",
        ".",
        "--use-deprecated=legacy-resolver",
        "simplewheel",
        allow_stderr_warning=True,
    )
    assert NOT_IMPLEMENTED not in result.stderr
    result.did_create(Path("scratch") / "simplewheel-2.0-1-py2.py3-none-any.whl")


def test_the_removed_flag_is_rejected(script: PipTestEnvironment) -> None:
    result = script.pip(
        "install", "--use-feature=nab-resolver", "simplewheel", expect_error=True
    )
    assert "invalid choice: 'nab-resolver'" in result.stderr
