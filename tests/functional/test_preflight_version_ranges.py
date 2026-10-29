import textwrap
from pathlib import Path

import pytest

from tests.lib import PipTestEnvironment, create_basic_sdist_for_package
from tests.lib.wheel import make_wheel

from .test_static_dependency_preflight import create_prefix_sdist


@pytest.mark.parametrize("constraint", [False, True])
def test_disjoint_fixed_dependency_ranges_backtrack_without_backend(
    script: PipTestEnvironment, constraint: bool
) -> None:
    create_prefix_sdist(script, "1.0", "dependency<2", dynamic=False)
    create_prefix_sdist(script, "2.0", "dependency>=2", dynamic_python=True)
    make_wheel("dependency", "1.0").save_to_dir(script.scratch_path)
    options = (
        ["--constraint", script.temporary_file("constraints.txt", "dependency<2\n")]
        if constraint
        else ["dependency<2"]
    )

    result = script.pip(
        "download",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--dest",
        "downloaded",
        "prefix-demo",
        *options,
    )

    result.did_create(Path("scratch/downloaded/prefix_demo-1.0.tar.gz"))
    result.did_create(Path("scratch/downloaded/dependency-1.0-py2.py3-none-any.whl"))


def test_overlapping_ranges_allow_backend_to_reveal_hidden_dependency_url(
    script: PipTestEnvironment,
) -> None:
    hidden = script.scratch_path / "hidden"
    hidden.mkdir()
    wheel = Path(make_wheel("dependency", "1.0").save_to_dir(hidden))
    make_wheel("dependency", "2.0").save_to_dir(script.scratch_path)
    helper = Path(
        make_wheel(
            "helper",
            "1",
            metadata=(
                "Metadata-Version: 2.2\nName: helper\nVersion: 1\n"
                f"Requires-Dist: dependency @ {wheel.as_uri()}\n"
            ),
        ).save_to_dir(script.scratch_path)
    )
    constraint = script.temporary_file("constraints.txt", "dependency<2\n")
    event = script.scratch_path / "backend-ran"
    script.environ["PIP_TEST_RANGE_BACKEND"] = str(event)
    fixed = (
        "Metadata-Version: 2.6\nName: range-demo\nVersion: 1\n"
        "Requires-Dist: dependency>=1\n"
    )
    archive = create_basic_sdist_for_package(
        script,
        "range_demo",
        "1",
        extra_files={
            "PKG-INFO": fixed + "Dynamic: Requires-Dist\n",
            "complete-metadata": fixed
            + f"Requires-Dist: dependency @ {wheel.as_uri()}\n",
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
                    Path(os.environ["PIP_TEST_RANGE_BACKEND"]).touch()
                    info = Path(metadata_directory) / "range_demo-1.dist-info"
                    info.mkdir()
                    (info / "METADATA").write_bytes(
                        Path("complete-metadata").read_bytes()
                    )
                    return info.name
                """),
        },
    )

    result = script.pip(
        "download",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--dest",
        "downloaded",
        "--constraint",
        constraint,
        helper.as_uri(),
        archive.as_uri(),
    )

    assert event.exists()
    result.did_create(Path("scratch/downloaded/dependency-1.0-py2.py3-none-any.whl"))


@pytest.mark.parametrize("candidate_version", ["1", "1.0"])
def test_arbitrary_version_domain_backtracks_without_header_text_assumptions(
    script: PipTestEnvironment, candidate_version: str
) -> None:
    create_prefix_sdist(script, "1.0", "dependency==1.0", dynamic=False)
    create_prefix_sdist(script, "2.0", "dependency>=2", dynamic_python=True)
    make_wheel("dependency", candidate_version).save_to_dir(script.scratch_path)

    result = script.pip(
        "download",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--dest",
        "downloaded",
        "prefix-demo",
        f"dependency==={candidate_version}",
    )

    result.did_create(Path("scratch/downloaded/prefix_demo-1.0.tar.gz"))
    result.did_create(
        Path(f"scratch/downloaded/dependency-{candidate_version}-py2.py3-none-any.whl")
    )
