"""Keep the explicitly requested source mode for matching dependency URLs."""

import json
from pathlib import Path

import pytest

from tests.functional.test_nab_preparation_reuse import tracked_install
from tests.lib import PipTestEnvironment
from tests.lib.wheel import make_wheel

BACKEND = """from pathlib import Path
from zipfile import ZipFile

METADATA = "Metadata-Version: 2.1\\nName: dep\\nVersion: 1\\n"

def get_requires_for_build_wheel(config_settings=None):
    return []

def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    info = Path(metadata_directory) / "dep-1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(METADATA)
    return info.name

def wheel(directory, editable):
    output = Path(directory) / "dep-1-py3-none-any.whl"
    with ZipFile(output, "w") as archive:
        archive.writestr("dep-1.dist-info/METADATA", METADATA)
        archive.writestr("dep-1.dist-info/WHEEL", "Wheel-Version: 1.0\\n"
                        "Root-Is-Purelib: true\\nTag: py3-none-any\\n")
        archive.writestr("dep-1.dist-info/RECORD", "")
        if editable:
            archive.writestr("dep.pth", str(Path(__file__).parent) + "\\n")
        else:
            archive.writestr("dep.py", (Path(__file__).parent / "dep.py").read_bytes())
    return output.name

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    return wheel(wheel_directory, False)

def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    return wheel(wheel_directory, True)

get_requires_for_build_editable = get_requires_for_build_wheel
prepare_metadata_for_build_editable = prepare_metadata_for_build_wheel
"""


def directory_source(path: Path) -> None:
    """Create a dependency whose editable wheel reads the live source module."""
    path.mkdir()
    (path / "backend.py").write_text(BACKEND)
    (path / "dep.py").write_text("VALUE = 1\n")
    (path / "pyproject.toml").write_text(
        "[build-system]\nrequires = []\n"
        'build-backend = "backend"\nbackend-path = ["."]\n'
    )


@pytest.mark.parametrize("editable", [False, True])
def test_matching_dependency_url_uses_the_fixed_source_mode(
    script: PipTestEnvironment, *, editable: bool
) -> None:
    source = script.scratch_path / "source"
    directory_source(source)
    metadata = "Metadata-Version: 2.1\nName: app\nVersion: 1\n"
    metadata += f"Requires-Dist: dep @ {source.as_uri()}\n"
    make_wheel("app", "1", metadata=metadata).save_to_dir(script.scratch_path)
    roots = ("-e", source.as_uri(), "app") if editable else (source.as_uri(), "app")

    result = tracked_install(script, *roots, options=("--no-build-isolation",))

    assert "NATIVE True" not in result.stdout
    assert "NATIVE False" not in result.stdout
    script.assert_installed(app="1", dep="1")
    script.pip("check")
    origin = script.site_packages_path / "dep-1.dist-info" / "direct_url.json"
    assert json.loads(origin.read_text())["dir_info"].get("editable", False) is editable

    (source / "dep.py").write_text("VALUE = 2\n")
    imported = script.run("python", "-c", "import dep; print(dep.VALUE)")
    assert imported.stdout.strip() == ("2" if editable else "1")


def test_fixed_editable_source_does_not_override_a_different_directory(
    script: PipTestEnvironment,
) -> None:
    source = script.scratch_path / "source"
    other = script.scratch_path / "other"
    directory_source(source)
    directory_source(other)
    metadata = "Metadata-Version: 2.1\nName: app\nVersion: 1\n"
    metadata += f"Requires-Dist: dep @ {other.as_uri()}\n"
    make_wheel("app", "1", metadata=metadata).save_to_dir(script.scratch_path)

    tracked_install(
        script,
        "-e",
        source.as_uri(),
        "app",
        options=("--no-build-isolation",),
        expect_error=True,
    )

    script.assert_not_installed("app", "dep")
