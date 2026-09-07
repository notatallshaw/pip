from tests.lib import (
    PipTestEnvironment,
    create_basic_sdist_for_package,
    create_basic_wheel_for_package,
)
from tests.lib.wheel import make_wheel


def test_prefer_binary_rechecks_global_order_after_bad_wheel(
    script: PipTestEnvironment,
) -> None:
    """Prefer an older wheel over a newer sdist after rejecting a bad wheel."""
    create_basic_wheel_for_package(script, "pkg", "1.0")
    create_basic_sdist_for_package(script, "pkg", "2.0")
    preferred = make_wheel(
        name="pkg", version="2.0", metadata_updates={"Version": "9.0"}
    )
    preferred.save_to(script.scratch_path / "pkg-2.0-py2.py3-none-any.whl")

    script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--prefer-binary",
        "--no-build-isolation",
        "pkg",
    )
    script.assert_installed(pkg="1.0")
