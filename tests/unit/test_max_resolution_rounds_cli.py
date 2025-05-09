"""Tests that the max_resolution_rounds parameter is properly passed through the CLI."""

from unittest import mock

from pip._internal.cli.req_command import RequirementCommand
from pip._internal.commands.install import InstallCommand


def test_max_resolution_rounds_passed_to_resolver() -> None:
    """Test max_resolution_rounds is passed from CLI options to the resolver."""

    # Create a mock for the resolver
    mock_resolver = mock.Mock()

    # Mock the make_resolver method to return our mock resolver
    with mock.patch.object(
        RequirementCommand, "make_resolver", return_value=mock_resolver
    ):
        # Create an instance of InstallCommand
        cmd = InstallCommand()

        # Create mock options with a custom max_resolution_rounds value
        mock_options = mock.Mock()
        mock_options.max_resolution_rounds = 5000
        mock_options.ignore_installed = False
        mock_options.ignore_requires_python = False
        mock_options.force_reinstall = False
        mock_options.upgrade_strategy = "to-satisfy-only"
        mock_options.use_user_site = False
        mock_options.isolated_mode = False
        mock_options.ignore_dependencies = False
        mock_options.features_enabled = []
        mock_options.use_pep517 = None

        # Call make_resolver
        resolver = cmd.make_resolver(
            preparer=mock.Mock(),
            finder=mock.Mock(),
            options=mock_options,
            wheel_cache=None,
        )

        # Assert that the resolver was called with our options
        assert resolver is mock_resolver

        # Get the call arguments
        RequirementCommand.make_resolver.assert_called_once()
        args, kwargs = RequirementCommand.make_resolver.call_args

        # Check that max_resolution_rounds was passed with the custom value
        assert kwargs.get("max_resolution_rounds") == 5000
