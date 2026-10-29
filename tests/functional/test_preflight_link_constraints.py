from pathlib import Path

import pytest

from tests.lib import PipTestEnvironment, create_basic_sdist_for_package
from tests.lib.wheel import make_wheel


@pytest.mark.parametrize("requires_conflicted", [False, True])
def test_conflicting_link_constraints_reject_only_required_projects(
    script: PipTestEnvironment, requires_conflicted: bool
) -> None:
    make_wheel("leaf", "1.0").save_to_dir(script.scratch_path)
    for version, dependency in (("1.0", "leaf==1.0"), ("2.0", "dependency")):
        if version == "2.0" and not requires_conflicted:
            continue
        create_basic_sdist_for_package(
            script,
            "link_demo",
            version,
            extra_files={
                "PKG-INFO": (
                    "Metadata-Version: 2.6\nName: link-demo\n"
                    f"Version: {version}\nRequires-Dist: {dependency}\n"
                    + ("Dynamic: Requires-Dist\n" if version == "2.0" else "")
                ),
                "pyproject.toml": (
                    "[build-system]\nrequires = []\n"
                    'build-backend = "unavailable_backend"\n'
                ),
            },
        )
    urls = [
        (script.scratch_path / f"dependency-{version}-py3-none-any.whl").as_uri()
        for version in ("1.0", "2.0")
    ]
    constraints = script.temporary_file(
        "constraints.txt", "".join(f"dependency @ {url}\n" for url in urls)
    )

    result = script.pip(
        "download",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--constraint",
        constraints,
        "--dest",
        "downloaded",
        "link-demo",
    )

    result.did_create(Path("scratch/downloaded/link_demo-1.0.tar.gz"))


def test_url_prefix_does_not_use_plain_name_link_domain_proof(
    script: PipTestEnvironment,
) -> None:
    urls = [
        (script.scratch_path / f"dependency-{version}-py3-none-any.whl").as_uri()
        for version in ("1.0", "2.0")
    ]
    create_basic_sdist_for_package(
        script,
        "link_demo",
        "2.0",
        extra_files={
            "PKG-INFO": (
                "Metadata-Version: 2.6\nName: link-demo\nVersion: 2.0\n"
                f"Requires-Dist: dependency @ {urls[0]}\nDynamic: Requires-Dist\n"
            ),
            "pyproject.toml": (
                '[build-system]\nrequires = []\nbuild-backend = "unavailable_backend"\n'
            ),
        },
    )
    constraints = script.temporary_file(
        "constraints.txt", "".join(f"dependency @ {url}\n" for url in urls)
    )

    result = script.pip(
        "download",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--constraint",
        constraints,
        "link-demo",
        expect_error=True,
    )

    assert "backend-unavailable" in result.stderr
