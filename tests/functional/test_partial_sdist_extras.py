from pathlib import Path

from tests.lib import PipTestEnvironment, create_basic_sdist_for_package


def test_rejected_source_does_not_claim_unresolved_extra_is_missing(
    script: PipTestEnvironment,
) -> None:
    for version, fields in (
        ("1.0", "Requires-Python: >=3.10\nProvides-Extra: speed\n"),
        (
            "2.0",
            "Requires-Python: >=99\nDynamic: Provides-Extra\nDynamic: Requires-Dist\n",
        ),
    ):
        create_basic_sdist_for_package(
            script,
            "unresolved_demo",
            version,
            extra_files={
                "PKG-INFO": (
                    "Metadata-Version: 2.2\nName: unresolved-demo\n"
                    f"Version: {version}\n{fields}\n"
                ),
                "pyproject.toml": (
                    "[build-system]\nrequires = []\n"
                    'build-backend = "unavailable_backend"\n'
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
        "unresolved-demo[speed]",
    )

    result.did_create(Path("scratch/downloaded/unresolved_demo-1.0.tar.gz"))
    assert "does not provide the extra" not in result.stderr


def test_complete_backend_metadata_still_warns_for_missing_extra(
    script: PipTestEnvironment,
) -> None:
    archive = create_basic_sdist_for_package(
        script,
        "complete_demo",
        "1.0",
        extra_files={
            "PKG-INFO": (
                "Metadata-Version: 2.2\nName: complete-demo\nVersion: 1.0\n"
                "Dynamic: Provides-Extra\nDynamic: Requires-Dist\n"
            ),
            "pyproject.toml": (
                '[build-system]\nrequires = []\nbuild-backend = "backend"\n'
                'backend-path = ["."]\n'
            ),
            "backend.py": (
                "from pathlib import Path\n"
                "def prepare_metadata_for_build_wheel(\n"
                "    metadata_directory, config_settings=None\n"
                "):\n"
                "    info = Path(metadata_directory) / 'complete_demo-1.0.dist-info'\n"
                "    info.mkdir()\n"
                "    metadata = ('Metadata-Version: 2.2\\nName: complete-demo\\n'\n"
                "                'Version: 1.0\\nDynamic: Provides-Extra\\n')\n"
                "    (info / 'METADATA').write_text(metadata)\n"
                "    return info.name\n"
            ),
        },
    )

    result = script.pip(
        "download",
        "--no-index",
        "--dest",
        "downloaded",
        f"complete-demo[speed] @ {archive.as_uri()}",
        allow_stderr_warning=True,
    )

    assert "does not provide the extra 'speed'" in result.stderr
    result.did_create(Path("scratch/downloaded/complete_demo-1.0.tar.gz"))
