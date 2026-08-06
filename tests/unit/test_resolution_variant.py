"""Tests for selecting between pip's resolver variants."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from optparse import Values
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest

import pip._internal
import pip._internal.resolution.legacy.resolver
import pip._internal.resolution.nab.resolver
import pip._internal.resolution.resolvelib.resolver
from pip._internal.build_env import (
    InprocessBuildEnvironmentInstaller,
    SubprocessBuildEnvironmentInstaller,
)
from pip._internal.build_env.base import Prefix
from pip._internal.cli import cmdoptions
from pip._internal.cli.req_command import RequirementCommand
from pip._internal.commands import create_command
from pip._internal.exceptions import CommandError

from tests.lib import make_test_finder


@pytest.fixture(autouse=True)
def clean_append_option_defaults() -> Iterator[None]:
    """Isolate the parser defaults for the two resolver-selecting options.

    ``use_new_feature`` and ``use_deprecated_feature`` are ``partial``s whose
    ``default=[]`` was evaluated once at import, so every Option built from
    them shares one list and ``action="append"`` mutates it. Values therefore
    leak from one parsed command to the next inside a process. That is a
    pre-existing pip behaviour, not something this variant introduces, but it
    would make these tests order-dependent.
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
    def test_default_is_resolvelib(self) -> None:
        assert RequirementCommand.determine_resolver_variant(make_options()) == (
            "resolvelib"
        )

    def test_legacy_flag_selects_legacy(self) -> None:
        options = make_options(deprecated=["legacy-resolver"])
        assert RequirementCommand.determine_resolver_variant(options) == "legacy"

    def test_nab_flag_selects_nab(self) -> None:
        options = make_options(features=["nab-resolver"])
        assert RequirementCommand.determine_resolver_variant(options) == "nab"

    def test_unrelated_feature_does_not_select_nab(self) -> None:
        options = make_options(features=["fast-deps"])
        assert RequirementCommand.determine_resolver_variant(options) == "resolvelib"

    def test_both_resolver_flags_is_an_error(self) -> None:
        options = make_options(
            features=["nab-resolver"], deprecated=["legacy-resolver"]
        )
        with pytest.raises(CommandError, match="different resolvers"):
            RequirementCommand.determine_resolver_variant(options)


class TestUseFeatureFlagParsing:
    def test_nab_resolver_is_an_accepted_choice(self) -> None:
        choices = cmdoptions.use_new_feature().choices
        assert choices is not None
        assert "nab-resolver" in choices

    def test_install_accepts_the_flag(self) -> None:
        command = create_command("install", isolated=True)
        options, _ = command.parse_args(["--use-feature=nab-resolver", "somepkg"])
        assert options.features_enabled == ["nab-resolver"]

    def test_install_default_enables_nothing(self) -> None:
        command = create_command("install", isolated=True)
        options, _ = command.parse_args(["somepkg"])
        assert options.features_enabled == []
        assert RequirementCommand.determine_resolver_variant(options) == "resolvelib"

    @pytest.mark.parametrize("name", ["download", "wheel", "lock"])
    def test_other_resolving_commands_accept_the_flag(self, name: str) -> None:
        command = create_command(name, isolated=True)
        options, _ = command.parse_args(["--use-feature=nab-resolver", "somepkg"])
        assert RequirementCommand.determine_resolver_variant(options) == "nab"


class TestMakeResolver:
    def _make_resolver(self, options: Values) -> Any:
        return RequirementCommand.make_resolver(
            preparer=cast(Any, mock.Mock()),
            finder=cast(Any, mock.Mock()),
            options=options,
        )

    def test_default_is_resolvelib(self) -> None:
        resolver = self._make_resolver(make_options())
        assert isinstance(
            resolver, pip._internal.resolution.resolvelib.resolver.Resolver
        )

    def test_legacy_flag_gives_legacy_resolver(self) -> None:
        resolver = self._make_resolver(make_options(deprecated=["legacy-resolver"]))
        assert isinstance(resolver, pip._internal.resolution.legacy.resolver.Resolver)

    def test_nab_flag_gives_nab_resolver(self) -> None:
        resolver = self._make_resolver(make_options(features=["nab-resolver"]))
        assert isinstance(resolver, pip._internal.resolution.nab.resolver.Resolver)


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


class TestBuildEnvironmentInstallerVariant:
    @mock.patch("pip._internal.build_env.installer.call_subprocess")
    def test_subprocess_installer_does_not_forward_by_default(
        self, mock_call_subprocess: mock.Mock, tmp_path: Path
    ) -> None:
        installer = SubprocessBuildEnvironmentInstaller(make_test_finder())
        installer.install(
            requirements=["setuptools"],
            prefix=Prefix(str(tmp_path)),
            kind="build dependencies",
            for_req=None,
        )
        args = mock_call_subprocess.call_args.args[0]
        assert "nab-resolver" not in args

    @mock.patch("pip._internal.build_env.installer.call_subprocess")
    def test_subprocess_installer_forwards_the_flag(
        self, mock_call_subprocess: mock.Mock, tmp_path: Path
    ) -> None:
        installer = SubprocessBuildEnvironmentInstaller(
            make_test_finder(), resolver_variant="nab"
        )
        installer.install(
            requirements=["setuptools"],
            prefix=Prefix(str(tmp_path)),
            kind="build dependencies",
            for_req=None,
        )
        args = mock_call_subprocess.call_args.args[0]
        assert args[args.index("--use-feature") + 1] == "nab-resolver"
        # The flag must land before the -- separator, or it is a requirement.
        assert args.index("--use-feature") < args.index("--")

    @pytest.mark.parametrize(
        "inprocess, installer_name",
        [
            (False, "SubprocessBuildEnvironmentInstaller"),
            (True, "InprocessBuildEnvironmentInstaller"),
        ],
    )
    @pytest.mark.parametrize(
        "features, expected",
        [
            ([], "resolvelib"),
            (["nab-resolver"], "nab"),
        ],
    )
    def test_preparer_passes_the_variant_to_the_installer(
        self,
        tmp_path: Path,
        inprocess: bool,
        installer_name: str,
        features: list[str],
        expected: str,
    ) -> None:
        options = make_options(features=list(features))
        if inprocess:
            options.features_enabled = [
                *options.features_enabled,
                "inprocess-build-deps",
            ]
        options.src_dir = str(tmp_path / "src")
        options.build_isolation = True
        options.check_build_deps = False
        options.progress_bar = "off"
        options.require_hashes = False
        options.build_constraints = []
        options.cache_dir = str(tmp_path / "cache")

        with (
            mock.patch("pip._internal.cli.req_command.RequirementPreparer"),
            mock.patch(f"pip._internal.cli.req_command.{installer_name}") as installer,
        ):
            RequirementCommand.make_requirement_preparer(
                temp_build_dir=cast(Any, mock.Mock(path=str(tmp_path / "build"))),
                options=options,
                build_tracker=cast(Any, mock.Mock()),
                session=cast(Any, mock.Mock()),
                finder=cast(Any, mock.Mock()),
                allow_editables=True,
                use_user_site=False,
            )
        assert installer.call_args.kwargs["resolver_variant"] == expected

    def _inprocess_installer(
        self, variant: str, tmp_path: Path
    ) -> InprocessBuildEnvironmentInstaller:
        from pip._internal.cache import WheelCache
        from pip._internal.operations.build.build_tracker import BuildTracker

        return InprocessBuildEnvironmentInstaller(
            finder=make_test_finder(),
            build_tracker=cast(Any, mock.Mock(spec=BuildTracker)),
            wheel_cache=WheelCache(str(tmp_path / "cache")),
            resolver_variant=variant,
        )

    def test_inprocess_installer_defaults_to_resolvelib(self, tmp_path: Path) -> None:
        installer = self._inprocess_installer("resolvelib", tmp_path)
        resolver = installer._make_resolver()
        assert isinstance(
            resolver, pip._internal.resolution.resolvelib.resolver.Resolver
        )

    def test_inprocess_installer_honours_nab(self, tmp_path: Path) -> None:
        installer = self._inprocess_installer("nab", tmp_path)
        resolver = installer._make_resolver()
        assert isinstance(resolver, pip._internal.resolution.nab.resolver.Resolver)


@contextmanager
def nab_unimported() -> Iterator[None]:
    """Drop the nab package from sys.modules and restore it afterwards."""
    dropped = {
        name: module
        for name, module in sys.modules.items()
        if name.startswith("pip._internal.resolution.nab")
    }
    for name in dropped:
        del sys.modules[name]
    try:
        yield
    finally:
        sys.modules.update(dropped)


def nab_was_imported() -> bool:
    return any(name.startswith("pip._internal.resolution.nab") for name in sys.modules)


class TestNabIsUnreachableWithoutTheFlag:
    """Every import of the nab package sits behind the variant check."""

    def test_make_resolver_default(self) -> None:
        with nab_unimported():
            RequirementCommand.make_resolver(
                preparer=cast(Any, mock.Mock()),
                finder=cast(Any, mock.Mock()),
                options=make_options(),
            )
            assert not nab_was_imported()

    def test_make_resolver_legacy(self) -> None:
        with nab_unimported():
            RequirementCommand.make_resolver(
                preparer=cast(Any, mock.Mock()),
                finder=cast(Any, mock.Mock()),
                options=make_options(deprecated=["legacy-resolver"]),
            )
            assert not nab_was_imported()

    def test_inprocess_build_env_default(self, tmp_path: Path) -> None:
        from pip._internal.cache import WheelCache
        from pip._internal.operations.build.build_tracker import BuildTracker

        with nab_unimported():
            installer = InprocessBuildEnvironmentInstaller(
                finder=make_test_finder(),
                build_tracker=cast(Any, mock.Mock(spec=BuildTracker)),
                wheel_cache=WheelCache(str(tmp_path / "cache")),
            )
            installer._make_resolver()
            assert not nab_was_imported()

    def test_only_the_variant_check_reaches_the_package(self) -> None:
        """No module outside the guarded branches imports nab at all."""
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
