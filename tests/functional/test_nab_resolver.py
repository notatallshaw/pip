"""End to end checks that --use-feature=nab-resolver selects the nab variant.

The nab variant is plumbing only for now, so a run that reaches it stops with
a NotImplementedError. These tests assert that it is reached when the flag is
passed and that the same command without the flag is untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.lib import PipTestEnvironment, TestData

NOT_IMPLEMENTED = "The nab resolver is not implemented yet"


@pytest.mark.parametrize("command", ["download", "wheel", "install"])
def test_flag_reaches_the_nab_variant(
    script: PipTestEnvironment, data: TestData, command: str
) -> None:
    args: list[str] = [command, "--no-index", "-f", str(data.packages)]
    if command == "download":
        args += ["-d", "."]
    elif command == "wheel":
        args += ["-w", "."]
    result = script.pip(
        *args,
        "--use-feature=nab-resolver",
        "simplewheel",
        expect_error=True,
        allow_stderr_error=True,
    )
    assert NOT_IMPLEMENTED in result.stderr, str(result)


def test_lock_reaches_the_nab_variant(
    script: PipTestEnvironment, data: TestData
) -> None:
    result = script.pip(
        "lock",
        "--no-index",
        "-f",
        str(data.packages),
        "-o",
        "pylock.toml",
        "--use-feature=nab-resolver",
        "simplewheel",
        expect_error=True,
        allow_stderr_error=True,
    )
    assert NOT_IMPLEMENTED in result.stderr, str(result)


def test_default_is_unchanged(script: PipTestEnvironment, data: TestData) -> None:
    result = script.pip(
        "download", "--no-index", "-f", str(data.packages), "-d", ".", "simplewheel"
    )
    assert NOT_IMPLEMENTED not in result.stderr
    result.did_create(Path("scratch") / "simplewheel-2.0-1-py2.py3-none-any.whl")


def test_legacy_resolver_is_unchanged(
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


def test_both_resolver_flags_is_a_clean_error(
    script: PipTestEnvironment, data: TestData
) -> None:
    result = script.pip(
        "download",
        "--no-index",
        "-f",
        str(data.packages),
        "-d",
        ".",
        "--use-feature=nab-resolver",
        "--use-deprecated=legacy-resolver",
        "simplewheel",
        expect_error=True,
    )
    assert "they select different resolvers" in result.stderr
    assert "Traceback" not in result.stderr


def test_unknown_feature_value_is_still_rejected(script: PipTestEnvironment) -> None:
    result = script.pip(
        "install", "--use-feature=nab", "simplewheel", expect_error=True
    )
    assert "invalid choice: 'nab'" in result.stderr
