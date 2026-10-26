from pathlib import Path

import pytest

from tests.lib import PipTestEnvironment, create_basic_sdist_for_package
from tests.lib.wheel import make_wheel


def create_identity_sdist(
    script: PipTestEnvironment, version: str, fields: str
) -> Path:
    return create_basic_sdist_for_package(
        script,
        "point_demo",
        version,
        extra_files={
            "PKG-INFO": (
                "Metadata-Version: 2.2\nName: point-demo\n"
                f"Version: {version}\n{fields}\n"
            ),
            "pyproject.toml": (
                '[build-system]\nrequires = []\nbuild-backend = "backend"\n'
                'backend-path = ["."]\n'
            ),
            "backend.py": 'raise RuntimeError("unneeded identity backend executed")\n',
        },
    )


@pytest.mark.parametrize("named", [False, True])
def test_fixed_source_version_rejects_ordinary_constraint_without_backend(
    script: PipTestEnvironment, named: bool
) -> None:
    archive = create_identity_sdist(
        script, "2.0", "Dynamic: Requires-Dist\nDynamic: Requires-Python\n"
    )
    constraint = script.temporary_file("constraints.txt", "point-demo<2\n")
    source = f"point-demo @ {archive.as_uri()}" if named else archive.as_uri()

    result = script.pip(
        "download",
        "--no-index",
        "--constraint",
        constraint,
        source,
        expect_error=True,
    )

    assert "unneeded identity backend executed" not in result.stderr
    assert "ResolutionImpossible" in result.stderr


def test_fixed_source_name_mismatch_does_not_execute_backend(
    script: PipTestEnvironment,
) -> None:
    archive = create_identity_sdist(
        script, "2.0", "Dynamic: Requires-Dist\nDynamic: Requires-Python\n"
    )

    result = script.pip(
        "download",
        "--no-index",
        f"different-name @ {archive.as_uri()}",
        expect_error=True,
    )

    assert "unneeded identity backend executed" not in result.stderr
    assert "inconsistent name" in result.stdout


def test_fixed_self_dependency_rejects_without_backend(
    script: PipTestEnvironment,
) -> None:
    create_identity_sdist(script, "1.0", "")
    create_identity_sdist(
        script, "2.0", "Requires-Dist: point-demo<2\nDynamic: Requires-Python\n"
    )

    result = script.pip(
        "download",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--dest",
        "downloaded",
        "point-demo",
    )

    result.did_create(Path("scratch/downloaded/point_demo-1.0.tar.gz"))


def test_arbitrary_equality_does_not_compare_header_version_spelling(
    script: PipTestEnvironment,
) -> None:
    make_wheel("mandatory", "1.0").save_to_dir(script.scratch_path)
    event = script.scratch_path / "backend-ran"
    script.environ["PIP_TEST_POINT_BACKEND"] = str(event)
    create_basic_sdist_for_package(
        script,
        "point_demo",
        "1",
        extra_files={
            "PKG-INFO": (
                "Metadata-Version: 2.2\nName: point-demo\nVersion: 1.0\n"
                "Dynamic: Requires-Dist\n"
            ),
            "pyproject.toml": (
                '[build-system]\nrequires = []\nbuild-backend = "backend"\n'
                'backend-path = ["."]\n'
            ),
            "backend.py": (
                "import os\nfrom pathlib import Path\n"
                "def prepare_metadata_for_build_wheel(\n"
                "    metadata_directory, config_settings=None\n"
                "):\n"
                "    Path(os.environ['PIP_TEST_POINT_BACKEND']).touch()\n"
                "    info = Path(metadata_directory) / 'point_demo-1.0.dist-info'\n"
                "    info.mkdir()\n"
                "    metadata = ('Metadata-Version: 2.2\\nName: point-demo\\n'\n"
                "                'Version: 1.0\\nRequires-Dist: mandatory==1.0\\n')\n"
                "    (info / 'METADATA').write_text(metadata)\n"
                "    return info.name\n"
            ),
        },
    )

    result = script.pip(
        "download",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--dest",
        "downloaded",
        "point-demo===1",
        allow_stderr_warning=True,
    )

    assert event.exists()
    result.did_create(Path("scratch/downloaded/mandatory-1.0-py2.py3-none-any.whl"))
