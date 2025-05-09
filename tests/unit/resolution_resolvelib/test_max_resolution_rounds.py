"""Tests for the use of max_resolution_rounds parameter in resolvelib resolver."""

from unittest import mock

from pip._internal.resolution.resolvelib.resolver import Resolver


def test_resolver_constructor_default() -> None:
    """Test that the default value for max_resolution_rounds is set correctly."""
    resolver = Resolver(
        preparer=mock.Mock(),
        finder=mock.Mock(),
        wheel_cache=None,
        make_install_req=mock.Mock(),
        use_user_site=False,
        ignore_dependencies=False,
        ignore_installed=False,
        ignore_requires_python=False,
        force_reinstall=False,
        upgrade_strategy="to-satisfy-only",
    )

    # Check that the default value is set to 200000
    assert resolver.max_resolution_rounds == 200000


def test_resolver_constructor_custom_value() -> None:
    """Test that a custom max_resolution_rounds value can be set."""
    custom_value = 5000
    resolver = Resolver(
        preparer=mock.Mock(),
        finder=mock.Mock(),
        wheel_cache=None,
        make_install_req=mock.Mock(),
        use_user_site=False,
        ignore_dependencies=False,
        ignore_installed=False,
        ignore_requires_python=False,
        force_reinstall=False,
        upgrade_strategy="to-satisfy-only",
        max_resolution_rounds=custom_value,
    )

    # Check that the custom value is set correctly
    assert resolver.max_resolution_rounds == custom_value


def test_resolver_uses_max_rounds_parameter() -> None:
    """Test that resolve method passes max_resolution_rounds to the resolver."""
    custom_value = 5000

    with mock.patch(
        "pip._vendor.resolvelib.resolvers.Resolver.resolve"
    ) as mock_resolve:
        resolver = Resolver(
            preparer=mock.Mock(),
            finder=mock.Mock(),
            wheel_cache=None,
            make_install_req=mock.Mock(),
            use_user_site=False,
            ignore_dependencies=False,
            ignore_installed=False,
            ignore_requires_python=False,
            force_reinstall=False,
            upgrade_strategy="to-satisfy-only",
            max_resolution_rounds=custom_value,
        )

        mock_result = mock.MagicMock()
        mock_resolve.return_value = mock_result
        mock_result.mapping = {}

        with mock.patch.object(
            resolver.factory, "collect_root_requirements"
        ) as mock_collect:
            mock_collected = mock.MagicMock()
            mock_collected.requirements = ["mock_req"]
            mock_collected.constraints = {}
            mock_collected.user_requested = {}
            mock_collect.return_value = mock_collected

            with (
                mock.patch("pip._internal.resolution.resolvelib.provider.PipProvider"),
                mock.patch("pip._internal.resolution.resolvelib.reporter.PipReporter"),
                mock.patch(
                    "os.environ",
                    {"PIP_RESOLVER_DEBUG": "0"},
                ),
            ):
                try:
                    resolver.resolve([mock.MagicMock()], check_supported_wheels=False)
                except Exception:
                    # We expect this to fail due to incomplete mocks
                    pass

    mock_resolve.assert_called_once()
    kwargs = mock_resolve.call_args.kwargs
    assert "max_rounds" in kwargs
    assert kwargs["max_rounds"] == custom_value
