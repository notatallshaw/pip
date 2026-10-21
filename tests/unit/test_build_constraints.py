"""Tests for build constraints functionality."""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from unittest import mock

import pytest

from pip._internal.build_env import SubprocessBuildEnvironmentInstaller
from pip._internal.build_env.base import Prefix
from pip._internal.cli.req_command import RequirementCommand
from pip._internal.commands import create_command
from pip._internal.exceptions import CommandError, InstallationError
from pip._internal.network.session import PipSession
from pip._internal.utils.deprecation import PipDeprecationWarning
from pip._internal.utils.temp_dir import TempDirectoryTypeRegistry

from tests.lib import make_test_finder


def test_build_constraints_use_command_session() -> None:
    constraint_url = "https://constraints.example/build.txt"
    command = create_command("download")
    assert isinstance(command, RequirementCommand)
    options, _ = command.parse_args(["--build-constraint", constraint_url])

    with (
        PipSession() as session,
        mock.patch.object(
            command, "get_default_session", return_value=session
        ) as get_default_session,
        mock.patch(
            "pip._internal.cli.req_command.get_file_content"
        ) as get_file_content,
    ):
        command._check_build_constraints(options)

    get_file_content.assert_called_once_with(constraint_url, session)
    get_default_session.assert_called_once_with(options)


def test_build_constraints_require_build_isolation() -> None:
    command = create_command("download")
    assert isinstance(command, RequirementCommand)
    options, _ = command.parse_args(
        [
            "--build-constraint",
            "https://constraints.example/build.txt",
            "--no-build-isolation",
        ]
    )

    with pytest.raises(
        CommandError,
        match="--build-constraint cannot be used with --no-build-isolation",
    ):
        command._check_build_constraints(options)


@pytest.mark.parametrize("command_name", ["download", "install", "lock", "wheel"])
def test_commands_check_build_constraints_before_requirements(
    command_name: str,
    tmp_path: Path,
) -> None:
    command = create_command(command_name)
    assert isinstance(command, RequirementCommand)
    missing_constraint = tmp_path / "missing.txt"
    options, args = command.parse_args(
        ["--build-constraint", os.fspath(missing_constraint)]
    )
    command.tempdir_registry = TempDirectoryTypeRegistry()

    with (
        command.main_context(),
        pytest.raises(
            InstallationError,
            match="Could not open requirements file",
        ),
    ):
        command.run(options, args)


class TestSubprocessBuildEnvironmentInstaller:
    """Test SubprocessBuildEnvironmentInstaller build constraint handling."""

    @mock.patch("pip._internal.build_env.installer.call_subprocess")
    @mock.patch.dict(os.environ, {"PIP_CONSTRAINT": "constraints.txt"})
    def test_install_ignores_regular_constraints_by_default(
        self, mock_call_subprocess: mock.Mock, tmp_path: Path
    ) -> None:
        """Without build constraints, the isolated build environment ignores
        any inherited constraints (such as via PIP_CONSTRAINT) and emits no
        warning."""
        installer = SubprocessBuildEnvironmentInstaller(make_test_finder())
        prefix = Prefix(str(tmp_path))

        with warnings.catch_warnings():
            warnings.simplefilter("error", PipDeprecationWarning)
            installer.install(
                requirements=["setuptools"],
                prefix=prefix,
                kind="build dependencies",
                for_req=None,
            )

        mock_call_subprocess.assert_called_once()
        args = mock_call_subprocess.call_args.args[0]
        kwargs = mock_call_subprocess.call_args.kwargs
        assert "--use-feature" not in args
        assert kwargs.get("extra_environ") == {"_PIP_IN_BUILD_IGNORE_CONSTRAINTS": "1"}

    @mock.patch("pip._internal.build_env.installer.call_subprocess")
    @mock.patch.dict(os.environ, {"PIP_CONSTRAINT": "constraints.txt"})
    def test_install_passes_build_constraints(
        self, mock_call_subprocess: mock.Mock, tmp_path: Path
    ) -> None:
        """With build constraints, each file is forwarded with --build-constraint
        and the inherited-constraint ignore flag is set, so the subprocess
        ignores inherited regular constraints and applies the build constraints
        instead."""
        installer = SubprocessBuildEnvironmentInstaller(
            make_test_finder(),
            build_constraints=["build-constraints.txt"],
        )
        prefix = Prefix(str(tmp_path))

        installer.install(
            requirements=["setuptools"],
            prefix=prefix,
            kind="build dependencies",
            for_req=None,
        )

        mock_call_subprocess.assert_called_once()
        args = mock_call_subprocess.call_args.args[0]
        kwargs = mock_call_subprocess.call_args.kwargs
        assert "--use-feature" not in args
        assert "--constraint" not in args
        assert args[args.index("--build-constraint") + 1] == "build-constraints.txt"
        assert kwargs.get("extra_environ") == {"_PIP_IN_BUILD_IGNORE_CONSTRAINTS": "1"}
