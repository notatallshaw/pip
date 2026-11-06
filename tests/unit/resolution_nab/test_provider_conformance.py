"""Run the generic provider contracts through pip's local wheel candidates."""

from itertools import count
from pathlib import Path

from pip._vendor.nab_resolver.testing import (
    ProviderScenario,
    check_provider_scenarios,
)
from pip._vendor.nab_resolver.types import RangeProtocol
from pip._vendor.packaging.ranges import VersionRange
from pip._vendor.packaging.version import Version

from pip._internal.cli.req_command import RequirementCommand
from pip._internal.commands.install import InstallCommand
from pip._internal.index.collector import LinkCollector
from pip._internal.index.package_finder import PackageFinder
from pip._internal.models.search_scope import SearchScope
from pip._internal.models.selection_prefs import SelectionPreferences
from pip._internal.network.session import PipSession
from pip._internal.operations.build.build_tracker import get_build_tracker
from pip._internal.operations.prepare import RequirementPreparer
from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.nab.catalogue import CatalogueProvider
from pip._internal.resolution.nab.factory import Factory
from pip._internal.resolution.nab.provider import PipProvider
from pip._internal.utils.temp_dir import TempDirectory, global_tempdir_manager

from tests.lib.wheel import make_wheel

_PACKAGES = ("contractfirst", "contractsecond", "contractthird")
_VERSIONS = (Version("1"), Version("2"))
_SPECIFIERS = {
    (False, False): "<1",
    (True, False): "==1",
    (False, True): "==2",
    (True, True): "",
}


def _requirement(package: str, allowed: RangeProtocol[Version]) -> str:
    """Express membership in the two-version fixture as a standard requirement."""
    membership = (_VERSIONS[0] in allowed, _VERSIONS[1] in allowed)
    return package + _SPECIFIERS[membership]


def _write_wheels(scenario: ProviderScenario[str, Version], directory: Path) -> None:
    """Encode the independent graph in local wheel metadata for the real finder."""
    directory.mkdir()
    for package, versions in scenario.graph.items():
        for version, dependencies in versions.items():
            requires = [
                _requirement(child, allowed) for child, allowed in dependencies.items()
            ]
            make_wheel(
                package,
                str(version),
                metadata_updates={"Requires-Dist": requires},
            ).save_to_dir(directory)


def _catalogue(
    scenario: ProviderScenario[str, Version],
    finder: PackageFinder,
    preparer: RequirementPreparer,
) -> CatalogueProvider:
    """Give each resolution fresh native candidates, declarations and caches."""
    factory = Factory(
        finder=finder,
        preparer=preparer,
        make_install_req=install_req_from_line,
        wheel_cache=None,
        use_user_site=False,
        force_reinstall=False,
        ignore_installed=True,
        ignore_requires_python=False,
        py_version_info=None,
    )

    # Recreate the fixed inputs instead of reading the provider's parsed ranges.
    inputs = [
        install_req_from_line(_requirement(package, allowed))
        for package, allowed in scenario.requirements.items()
    ]
    inputs.extend(
        install_req_from_line(_requirement(package, allowed), constraint=True)
        for package, allowed in scenario.constraints.items()
    )
    collected = factory.collect_root_requirements(inputs)
    native = PipProvider(
        factory,
        constraints=collected.constraints,
        ignore_dependencies=False,
        upgrade_strategy="to-satisfy-only",
        user_requested=collected.user_requested,
    )
    return CatalogueProvider(factory, native, collected)


def test_local_wheel_provider_satisfies_generic_contracts(tmp_path: Path) -> None:
    options, _ = InstallCommand("x", "y").parse_args(["--no-build-isolation"])
    queries = count()

    # The session and tracker outlive all providers; candidate caches do not.
    with (
        global_tempdir_manager(),
        TempDirectory() as temporary,
        get_build_tracker() as tracker,
        PipSession() as session,
    ):

        def provider(scenario: ProviderScenario[str, Version]) -> CatalogueProvider:
            """Build one resolution over the independently encoded wheel graph."""
            directory = tmp_path / str(next(queries))
            _write_wheels(scenario, directory)

            finder = PackageFinder.create(
                LinkCollector(session, SearchScope([str(directory)], [], False)),
                SelectionPreferences(allow_yanked=False),
            )

            preparer = RequirementCommand.make_requirement_preparer(
                temporary,
                options=options,
                build_tracker=tracker,
                session=session,
                finder=finder,
                use_user_site=False,
                verbosity=0,
                allow_editables=True,
            )
            return _catalogue(scenario, finder, preparer)

        check_provider_scenarios(
            provider,
            packages=_PACKAGES,
            versions=_VERSIONS,
            range_type=VersionRange,
        )
