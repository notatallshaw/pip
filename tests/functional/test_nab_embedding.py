import json
import shutil
from pathlib import Path

import pytest

from tests.functional.test_nab_catalogue_yanked import yanked_listing
from tests.lib import PipTestEnvironment, create_basic_wheel_for_package
from tests.lib.wheel import make_wheel

_SHARED_PREFERENCE_PROBE = """\
import importlib
from pathlib import Path
import runpy
import sys
import pip._vendor as vendor

debundled = sys.argv.pop(1) == "1"
if debundled:
    # This fixture keeps metadata helpers bundled while making Nab external.
    for name in ("packaging", "pkg_resources"):
        sys.modules[name] = None

    initializer = Path(vendor.__file__).read_text()
    initializer = initializer.replace("DEBUNDLED = False", "DEBUNDLED = True")
    exec(compile(initializer, vendor.__file__, "exec"), vendor.__dict__)

shared = importlib.import_module("pip._vendor.nab_provider.preference")
errors = importlib.import_module("pip._vendor.nab_resolver.errors")
assert shared.ResolutionError is errors.ResolutionError
if debundled:
    canonical = importlib.import_module("nab_provider.preference")
    assert shared is canonical
    assert errors is importlib.import_module("nab_resolver.errors")
    assert Path(shared.__file__).parent.name == "nab_provider"
    assert "external" in Path(shared.__file__).parts

shared_provider = importlib.import_module(
    "pip._vendor.nab_provider.preference_provider"
)
assert shared_provider.ResolutionError is errors.ResolutionError
selections = []
select = shared_provider.PreferenceProvider.select
def select_and_record(self):
    selections.append(True)
    return select(self)
shared_provider.PreferenceProvider.select = select_and_record
try:
    runpy.run_module("pip", run_name="__main__", alter_sys=True)
finally:
    assert selections
"""


_PACKAGING_RANGES_PROBE = """\
import runpy
import sys
from pathlib import Path

import pip._vendor as vendor

external = sys.argv.pop(1) == "1"
if external:
    initializer = Path(vendor.__file__).read_text()
    initializer = initializer.replace("DEBUNDLED = False", "DEBUNDLED = True")
    exec(compile(initializer, vendor.__file__, "exec"), vendor.__dict__)

try:
    runpy.run_module("pip", run_name="__main__", alter_sys=True)
except SystemExit as result:
    if result.code in (None, 0):
        # Preserve pip's normal import order before checking the resulting types.
        from pip._vendor.packaging import ranges, specifiers, version

        if external:
            import packaging.ranges
            assert ranges is packaging.ranges

        allowed = specifiers.SpecifierSet(">=1").to_range()
        assert isinstance(allowed, ranges.VersionRange)
        selected = allowed & ranges.VersionRange.singleton(version.Version("2"))
        assert version.Version("2") in selected
    raise
"""


def test_direct_url_discovered_after_installed_candidate(
    script: PipTestEnvironment,
) -> None:
    """Read new dependencies from a direct artifact at an installed version."""
    wheel = create_basic_wheel_for_package(script, "dep", "1.0")
    script.pip("install", "--no-index", wheel)

    source = script.scratch_path / "direct"
    source.mkdir()
    replacement = create_basic_wheel_for_package(
        script, "dep", "1.0", depends=["child==1.0"]
    )
    replacement = replacement.rename(source / replacement.name)
    create_basic_wheel_for_package(script, "child", "1.0")
    app = make_wheel(
        name="app",
        version="1.0",
        metadata=(
            "Metadata-Version: 2.1\nName: app\nVersion: 1.0\n"
            f"Requires-Dist: dep @ {replacement.as_uri()}\n"
        ),
    )
    app.save_to(script.scratch_path / "app-1.0-py2.py3-none-any.whl")

    script.pip(
        "install", "--no-index", "--find-links", script.scratch_path, "dep", "app"
    )
    script.assert_installed(app="1.0", dep="1.0", child="1.0")


def test_try_another_file_when_preferred_wheel_has_inconsistent_metadata(
    script: PipTestEnvironment,
) -> None:
    """Try the next file after rejecting inconsistent wheel metadata."""
    create_basic_wheel_for_package(script, "pkg", "1.0")
    preferred = make_wheel(
        name="pkg", version="1.0", metadata_updates={"Version": "9.0"}
    )
    preferred.save_to(script.scratch_path / "pkg-1.0-2-py2.py3-none-any.whl")

    script.pip("install", "--no-index", "--find-links", script.scratch_path, "pkg")
    script.assert_installed(pkg="1.0")


@pytest.mark.parametrize("external", [False, True])
def test_packaging_ranges_share_the_dependency_namespace(
    script: PipTestEnvironment, external: bool
) -> None:
    """Resolve with matching bundled or external range-producing dependencies."""
    source = Path(__file__).parents[2] / "src/pip/_vendor"
    external_packages = script.scratch_path / "external"
    external_packages.mkdir()
    if external:
        # Matching dependency code keeps this a namespace compatibility check.
        for package in ("nab_resolver", "packaging", "pkg_resources"):
            shutil.copytree(
                source / package,
                external_packages / package,
                ignore=shutil.ignore_patterns("__pycache__"),
            )

    create_basic_wheel_for_package(script, "app", "1", depends=["dep>=1"])
    create_basic_wheel_for_package(script, "dep", "1")
    create_basic_wheel_for_package(script, "dep", "2")
    script.environ["PYTHONPATH"] = str(external_packages)
    probe = script.scratch_path / "packaging_ranges.py"
    probe.write_text(_PACKAGING_RANGES_PROBE)
    report = script.scratch_path / "selection.json"

    script.run(
        "python",
        str(probe),
        str(int(external)),
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        str(script.scratch_path),
        "--report",
        str(report),
        "app",
        allow_stderr_warning=True,
    )

    script.assert_installed(app="1", dep="2")
    selected = json.loads(report.read_text())["install"]
    assert {
        item["metadata"]["name"]: item["metadata"]["version"] for item in selected
    } == {"app": "1", "dep": "2"}


@pytest.mark.parametrize("debundled", [False, True])
def test_shared_preference_resolves_with_bundled_or_external_modules(
    script: PipTestEnvironment, debundled: bool
) -> None:
    """Use one shared preference implementation and one solver exception namespace."""
    source = Path(__file__).parents[2] / "src/pip/_vendor"
    external = script.scratch_path / "external"
    external.mkdir()
    if debundled:
        for package in ("nab_resolver", "nab_provider"):
            shutil.copytree(
                source / package,
                external / package,
                ignore=shutil.ignore_patterns("__pycache__"),
            )

        # Restore the canonical imports provided by the external distribution.
        for module in ("preference.py", "preference_provider.py"):
            preference = external / "nab_provider" / module
            preference.write_text(
                preference.read_text().replace(
                    "pip._vendor.nab_resolver", "nab_resolver"
                )
            )

    script.environ["PYTHONPATH"] = str(external)
    probe = script.scratch_path / "shared_preference.py"
    probe.write_text(_SHARED_PREFERENCE_PROBE)
    listing = yanked_listing(
        script,
        [("app", "1.0", ["dep==2.0"]), ("dep", "1.0", []), ("dep", "2.0", [])],
    )
    report = script.scratch_path / "selection.json"

    script.run(
        "python",
        str(probe),
        str(int(debundled)),
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        str(listing),
        "--report",
        str(report),
        "app",
        allow_stderr_warning=True,
    )

    script.assert_installed(app="1.0", dep="2.0")
    selected = json.loads(report.read_text())["install"]
    dependency = next(item for item in selected if item["metadata"]["name"] == "dep")
    assert dependency["is_yanked"]
