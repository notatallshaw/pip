"""Preserve the original error when reporting an exception during installation."""

import sys
from unittest.mock import patch

from pip._internal.cli.main import main as pip_main
from pip._internal.commands import install
from pip._internal.metadata import BaseEnvironment
from pip._internal.req import InstallationResult


def main() -> int:
    def summary(installed: list[InstallationResult], env: BaseEnvironment) -> str:
        assert install._ACTIVE_INSTALLATIONS
        exec(sys.argv[2])
        raise AssertionError("Expected reporting code to raise an exception")

    with patch.object(install, "installed_packages_summary", summary):
        return pip_main(["install", "--no-index", sys.argv[1]])


if __name__ == "__main__":
    sys.exit(main())
