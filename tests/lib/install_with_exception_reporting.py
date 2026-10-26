"""Exercise CLI rendering and programmatic propagation after installation."""

import io
import sys
from unittest.mock import patch

import pip
from pip._internal.cli.main import cli
from pip._internal.cli.main import main as pip_main
from pip._internal.commands import install
from pip._internal.exceptions import BlockedImportError
from pip._internal.metadata import BaseEnvironment
from pip._internal.req import InstallationResult


class ReportingError(RuntimeError):
    def __str__(self) -> str:
        assert install._ACTIVE_INSTALLATIONS
        try:
            __import__("after_install")
        except BlockedImportError:
            print("Import blocked during exception reporting")
        else:
            raise AssertionError("Import allowed during exception reporting")
        return super().__str__()


class BrokenStream(io.StringIO):
    def write(self, text: str) -> int:
        raise OSError("stderr unavailable")


failure = ReportingError("installation failed")


def summary(installed: list[InstallationResult], env: BaseEnvironment) -> str:
    if sys.argv[3] == "none":
        sys.stderr = None
    elif sys.argv[3] == "broken":
        sys.stderr = BrokenStream()
    raise failure


def main() -> int:
    entrypoints = {"cli": cli, "main": pip_main, "pip.main": pip.main}
    mode = sys.argv[2]
    previous_stderr = sys.stderr
    previous_hook = sys.excepthook
    caught_error = None
    try:
        with patch.object(install, "installed_packages_summary", summary):
            try:
                status = entrypoints[mode](
                    ["--debug", "install", "--no-index", sys.argv[1]]
                )
            except ReportingError as error:
                caught_error = error
            else:
                assert mode == "cli"
                assert status == 1
    finally:
        sys.stderr = previous_stderr
        sys.excepthook = previous_hook

    if mode != "cli":
        assert caught_error is failure
        print("Original exception propagated")
    assert not install._ACTIVE_INSTALLATIONS
    assert __import__("after_install").value == 42
    print("Import allowed after pip finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
