import textwrap
from pathlib import Path

import pytest

from tests.lib import PipTestEnvironment, create_basic_sdist_for_package
from tests.lib.wheel import make_wheel


def create_extra_sdist(
    script: PipTestEnvironment,
    *,
    metadata_version: str = "2.6",
    provided: bool = True,
    dynamic_provided: bool = False,
) -> Path:
    prefix = (
        f"Metadata-Version: {metadata_version}\nName: extra-demo\nVersion: 1\n"
        'Requires-Dist: dependency==2; extra == "speed"\n'
    )
    if provided:
        prefix += "Provides-Extra: speed\n"
    dynamic = "Dynamic: Requires-Dist\nDynamic: Requires-Python\n"
    if dynamic_provided:
        dynamic += "Dynamic: Provides-Extra\n"
    complete = (
        prefix
        if metadata_version == "2.6"
        else ("Metadata-Version: 2.2\nName: extra-demo\nVersion: 1\n")
    )
    return create_basic_sdist_for_package(
        script,
        "extra_demo",
        "1",
        extra_files={
            "PKG-INFO": prefix + dynamic,
            "complete-metadata": complete + "Requires-Dist: mandatory==1\n",
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
                    Path(os.environ["PIP_TEST_EXTRA_BACKEND"]).touch()
                    info = Path(metadata_directory) / "extra_demo-1.dist-info"
                    info.mkdir()
                    (info / "METADATA").write_bytes(
                        Path("complete-metadata").read_bytes()
                    )
                    return info.name
                """),
        },
    )


@pytest.mark.parametrize("source_first", [False, True])
@pytest.mark.parametrize("dynamic_provided", [False, True])
@pytest.mark.parametrize("request_kind", ["named-url", "anonymous-path"])
def test_fresh_linked_root_extra_conflict_skips_backend(
    script: PipTestEnvironment,
    source_first: bool,
    dynamic_provided: bool,
    request_kind: str,
) -> None:
    archive = create_extra_sdist(script, dynamic_provided=dynamic_provided)
    for version in ("1", "2"):
        make_wheel("dependency", version).save_to_dir(script.scratch_path)
    event = script.scratch_path / "backend-ran"
    script.environ["PIP_TEST_EXTRA_BACKEND"] = str(event)
    source = (
        f"extra-demo[speed] @ {archive.as_uri()}"
        if request_kind == "named-url"
        else f"{archive}[speed]"
    )
    roots = [source, "dependency==1"]
    if not source_first:
        roots.reverse()

    result = script.pip(
        "download",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--ignore-requires-python",
        *roots,
        expect_error=True,
    )

    assert not event.exists()
    assert "ResolutionImpossible" in result.stderr


@pytest.mark.parametrize("control", ["no-extra", "unknown-extra", "old-metadata"])
def test_unproven_linked_root_extra_still_prepares_complete_metadata(
    script: PipTestEnvironment, control: str
) -> None:
    archive = create_extra_sdist(
        script,
        metadata_version="2.5" if control == "old-metadata" else "2.6",
        provided=control != "unknown-extra",
        dynamic_provided=control == "unknown-extra",
    )
    for name in ("dependency", "mandatory"):
        make_wheel(name, "1").save_to_dir(script.scratch_path)
    event = script.scratch_path / "backend-ran"
    script.environ["PIP_TEST_EXTRA_BACKEND"] = str(event)
    extras = "" if control == "no-extra" else "[speed]"

    result = script.pip(
        "download",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--dest",
        "downloaded",
        f"extra-demo{extras} @ {archive.as_uri()}",
        "dependency==1",
        allow_stderr_warning=True,
    )

    assert event.exists()
    result.did_create(Path("scratch/downloaded/mandatory-1-py2.py3-none-any.whl"))
