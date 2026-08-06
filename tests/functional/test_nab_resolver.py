"""End to end checks that --use-feature=nab-resolver selects the nab variant.

Each command has to reach the engine and come back with an answer, and the
same command without the flag has to be untouched. ``NOT_IMPLEMENTED`` is
still asserted absent: it is the sentence the seam raised before the engine
was wired, so a run that prints it has lost the engine rather than the flag.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.lib import PipTestEnvironment, TestData

NOT_IMPLEMENTED = "nab is not vendored into pip"


@pytest.mark.parametrize("command", ["download", "wheel", "install"])
def test_flag_reaches_the_nab_variant(
    script: PipTestEnvironment, data: TestData, command: str
) -> None:
    args: list[str] = [command, "--no-index", "-f", str(data.packages)]
    if command == "download":
        args += ["-d", "."]
    elif command == "wheel":
        args += ["-w", "."]
    result = script.pip(*args, "--use-feature=nab-resolver", "simplewheel")
    assert NOT_IMPLEMENTED not in result.stderr, str(result)
    assert "simplewheel" in result.stdout, str(result)


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
        allow_stderr_warning=True,
    )
    assert NOT_IMPLEMENTED not in result.stderr, str(result)
    result.did_create(Path("scratch") / "pylock.toml")


def test_default_is_unchanged(script: PipTestEnvironment, data: TestData) -> None:
    result = script.pip(
        "download", "--no-index", "-f", str(data.packages), "-d", ".", "simplewheel"
    )
    assert NOT_IMPLEMENTED not in result.stderr
    result.did_create(Path("scratch") / "simplewheel-2.0-1-py2.py3-none-any.whl")


def test_legacy_resolver_is_unchanged(
    script: PipTestEnvironment, data: TestData
) -> None:
    # The legacy resolver and the nab variant are mutually exclusive, so the
    # session-wide variant has to come off before the deprecated flag goes on.
    script.environ.pop("PIP_USE_FEATURE", None)
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
