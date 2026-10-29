from __future__ import annotations

import textwrap
from pathlib import Path

from tests.lib import PipTestEnvironment, create_basic_sdist_for_package, make_wheel


def test_same_collection_link_alias_does_not_make_other_project_extras_mandatory(
    script: PipTestEnvironment,
) -> None:
    wheel = Path(
        make_wheel(
            "cached_name", "1.0", metadata_updates={"Provides-Extra": "speed"}
        ).save_to_dir(script.scratch_path)
    )
    for name in ("dependency", "mandatory"):
        make_wheel(name, "1.0").save_to_dir(script.scratch_path)
    metadata = (
        "Metadata-Version: 2.6\nName: other-name\nVersion: 1.0\n"
        'Requires-Dist: dependency==2; extra == "speed"\nProvides-Extra: speed\n'
    )
    create_basic_sdist_for_package(
        script,
        "other_name",
        "1.0",
        extra_files={
            "PKG-INFO": metadata + "Dynamic: Requires-Dist\n",
            "complete-metadata": metadata + "Requires-Dist: mandatory==1.0\n",
            "pyproject.toml": (
                '[build-system]\nrequires = []\nbuild-backend = "backend"\n'
                'backend-path = ["."]\n'
            ),
            "backend.py": textwrap.dedent("""\
                import os
                from pathlib import Path

                def prepare_metadata_for_build_wheel(
                    metadata_directory, config_settings=None
                ):
                    Path(os.environ["PIP_TEST_ALIAS_BACKEND"]).touch()
                    dist = Path(metadata_directory) / "other_name-1.0.dist-info"
                    dist.mkdir()
                    (dist / "METADATA").write_bytes(
                        Path("complete-metadata").read_bytes()
                    )
                    return dist.name
                """),
        },
    )
    event = script.scratch_path / "backend-ran"
    script.environ["PIP_TEST_ALIAS_BACKEND"] = str(event)

    result = script.pip(
        "download",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--dest",
        "downloaded",
        f"cached-name @ {wheel.as_uri()}",
        f"other-name[speed] @ {wheel.as_uri()}",
        "other-name",
        "dependency==1.0",
    )

    assert event.exists()
    result.did_create(Path("scratch/downloaded/mandatory-1.0-py2.py3-none-any.whl"))
