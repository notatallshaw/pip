"""Tests for selecting between pip's resolvers."""

from __future__ import annotations

from collections.abc import Iterator
from optparse import Values
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest

import pip._internal
import pip._internal.resolution.legacy.resolver
import pip._internal.resolution.nab.resolver
from pip._internal.build_env import (
    InprocessBuildEnvironmentInstaller,
    SubprocessBuildEnvironmentInstaller,
)
from pip._internal.build_env.base import Prefix
from pip._internal.cli import cmdoptions
from pip._internal.cli.req_command import RequirementCommand
from pip._internal.commands import create_command

from tests.lib import make_test_finder


@pytest.fixture(autouse=True)
def clean_append_option_defaults() -> Iterator[None]:
    """Isolate the parser defaults for the two resolver-selecting options.

    ``use_new_feature`` and ``use_deprecated_feature`` are ``partial``s whose
    ``default=[]`` was evaluated once at import, so every Option built from
    them shares one list and ``action="append"`` mutates it. Values therefore
    leak from one parsed command to the next inside a process.
    """
    shared = [
        cmdoptions.use_new_feature().default,
        cmdoptions.use_deprecated_feature().default,
    ]
    saved = [list(entries) for entries in shared]
    for entries in shared:
        entries.clear()
    yield
    for entries, previous in zip(shared, saved):
        entries[:] = previous


def make_options(
    *, features: list[str] | None = None, deprecated: list[str] | None = None
) -> Values:
    return Values(
        {
            "features_enabled": features or [],
            "deprecated_features_enabled": deprecated or [],
            "isolated_mode": False,
            "ignore_dependencies": False,
            "only_dependencies": False,
        }
    )


class TestDetermineResolverVariant:
    def test_default_is_nab(self) -> None:
        assert RequirementCommand.determine_resolver_variant(make_options()) == "nab"

    def test_legacy_flag_selects_legacy(self) -> None:
        options = make_options(deprecated=["legacy-resolver"])
        assert RequirementCommand.determine_resolver_variant(options) == "legacy"

    def test_unrelated_feature_does_not_change_the_resolver(self) -> None:
        options = make_options(features=["fast-deps"])
        assert RequirementCommand.determine_resolver_variant(options) == "nab"


class TestTheFeatureFlagIsGone:
    def test_nab_resolver_is_not_a_choice(self) -> None:
        choices = cmdoptions.use_new_feature().choices
        assert choices is not None
        assert "nab-resolver" not in choices

    def test_install_rejects_the_flag(self, capsys: pytest.CaptureFixture[str]) -> None:
        command = create_command("install", isolated=True)
        with pytest.raises(SystemExit):
            command.parse_args(["--use-feature=nab-resolver", "somepkg"])
        assert "invalid choice: 'nab-resolver'" in capsys.readouterr().err

    @pytest.mark.parametrize("name", ["download", "wheel", "lock"])
    def test_every_resolving_command_defaults_to_nab(self, name: str) -> None:
        command = create_command(name, isolated=True)
        options, _ = command.parse_args(["somepkg"])
        assert RequirementCommand.determine_resolver_variant(options) == "nab"


class TestMakeResolver:
    def _make_resolver(self, options: Values) -> Any:
        return RequirementCommand.make_resolver(
            preparer=cast(Any, mock.Mock()),
            finder=cast(Any, mock.Mock()),
            options=options,
        )

    def test_default_is_nab(self) -> None:
        resolver = self._make_resolver(make_options())
        assert isinstance(resolver, pip._internal.resolution.nab.resolver.Resolver)

    def test_legacy_flag_gives_legacy_resolver(self) -> None:
        resolver = self._make_resolver(make_options(deprecated=["legacy-resolver"]))
        assert isinstance(resolver, pip._internal.resolution.legacy.resolver.Resolver)


class TestNabResolverRunsTheEngine:
    def make_resolver(self) -> pip._internal.resolution.nab.resolver.Resolver:
        return pip._internal.resolution.nab.resolver.Resolver(
            preparer=cast(Any, mock.Mock()),
            finder=cast(Any, mock.Mock()),
            wheel_cache=None,
            make_install_req=cast(Any, mock.Mock()),
            use_user_site=False,
            ignore_dependencies=False,
            only_dependencies=False,
            ignore_installed=True,
            ignore_requires_python=False,
            force_reinstall=False,
            upgrade_strategy="to-satisfy-only",
        )

    def test_resolve_with_no_requirements_reaches_the_engine(self) -> None:
        resolver = self.make_resolver()
        req_set = resolver.resolve([], check_supported_wheels=True)
        assert req_set.all_requirements == []
        assert resolver.get_installation_order(req_set) == []

    def test_installation_order_requires_a_solve_first(self) -> None:
        with pytest.raises(AssertionError, match="must call resolve"):
            self.make_resolver().get_installation_order(cast(Any, mock.Mock()))


class TestBuildEnvironmentInstaller:
    @mock.patch("pip._internal.build_env.installer.call_subprocess")
    def test_subprocess_installer_forwards_no_resolver_flag(
        self, mock_call_subprocess: mock.Mock, tmp_path: Path
    ) -> None:
        """The child pip is this pip, so it resolves with nab on its own."""
        installer = SubprocessBuildEnvironmentInstaller(make_test_finder())
        installer.install(
            requirements=["setuptools"],
            prefix=Prefix(str(tmp_path)),
            kind="build dependencies",
            for_req=None,
        )
        args = mock_call_subprocess.call_args.args[0]
        assert "--use-feature" not in args

    def test_inprocess_installer_resolves_with_nab(self, tmp_path: Path) -> None:
        from pip._internal.cache import WheelCache
        from pip._internal.operations.build.build_tracker import BuildTracker

        installer = InprocessBuildEnvironmentInstaller(
            finder=make_test_finder(),
            build_tracker=cast(Any, mock.Mock(spec=BuildTracker)),
            wheel_cache=WheelCache(str(tmp_path / "cache")),
        )
        resolver = installer._make_resolver()
        assert isinstance(resolver, pip._internal.resolution.nab.resolver.Resolver)


class TestNabIsThePipResolver:
    def test_only_the_resolver_seam_reaches_the_package(self) -> None:
        """No module outside the two construction sites imports nab."""
        root = Path(pip._internal.__file__).parent
        offenders = []
        for path in root.rglob("*.py"):
            if "_vendor" in path.parts or "resolution/nab" in path.as_posix():
                continue
            if "resolution.nab" in path.read_text(encoding="utf-8"):
                offenders.append(path.relative_to(root).as_posix())
        assert sorted(offenders) == [
            "build_env/installer.py",
            "cli/req_command.py",
        ]
