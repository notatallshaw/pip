import pytest

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


def create_unfixed_url_dependency(script: PipTestEnvironment) -> None:
    """Build an index parent whose URL dependency requires contextual queries."""
    direct = script.scratch_path / "direct"
    direct.mkdir()
    supplied = create_basic_wheel_for_package(script, "leaf", "1")
    supplied = supplied.rename(direct / supplied.name)
    make_wheel(
        name="bridge",
        version="1",
        metadata=(
            "Metadata-Version: 2.2\nName: bridge\nVersion: 1\n"
            f"Requires-Dist: leaf @ {supplied.as_uri()}\n"
        ),
    ).save_to_dir(script.scratch_path)


@pytest.mark.parametrize("contextual", [False, True])
@pytest.mark.parametrize("required", [False, True])
def test_late_exact_version_text_uses_the_matching_artifact(
    script: PipTestEnvironment, contextual: bool, required: bool
) -> None:
    first = create_basic_wheel_for_package(script, "dep", "1")
    first.rename(script.scratch_path / "dep-1-2-py3-none-any.whl")
    other = create_basic_wheel_for_package(script, "dep", "1.0")
    other.rename(script.scratch_path / "dep-1.0-1-py3-none-any.whl")
    create_basic_wheel_for_package(script, "app", "1", depends=["dep==1"])
    create_basic_wheel_for_package(script, "other", "2", depends=["dep===1.0"])
    create_basic_wheel_for_package(script, "other", "1")
    roots = ["app", "other==2" if required else "other"]
    if contextual:
        create_unfixed_url_dependency(script)
        roots.append("bridge")

    result = script.pip(
        "install", "-v", "--no-index", "--find-links", script.scratch_path, *roots
    )

    if contextual:
        assert "Nab catalogue fallback" in result.stdout
    else:
        assert "Nab catalogue success" in result.stdout
        assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(app="1")
    if required:
        script.assert_installed(other="2", dep="1.0")
    script.pip("check")


def test_invalid_metadata_rejects_equivalent_version_text_in_the_same_group(
    script: PipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(script, "dep", "1")
    rejected = script.scratch_path / "dep-2-2-py3-none-any.whl"
    make_wheel(
        name="dep", version="2", metadata_updates={"Requires-Dist": "broken>=?"}
    ).save_to(rejected)
    alternative = create_basic_wheel_for_package(script, "dep", "2.0")
    alternative.rename(script.scratch_path / "dep-2.0-1-py3-none-any.whl")

    result = script.pip(
        "install",
        "-v",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "dep",
        allow_stderr_warning=True,
    )

    assert "Nab catalogue success" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    assert "Ignoring version 2 of dep since it has invalid metadata" in result.stderr
    script.assert_installed(dep="1")


@pytest.mark.parametrize("contextual", [False, True])
@pytest.mark.parametrize("source", ["root", "constraint", "dependency", "extra"])
def test_installed_version_text_does_not_satisfy_another_exact_text(
    script: PipTestEnvironment, source: str, contextual: bool
) -> None:
    installed = create_basic_wheel_for_package(script, "dep", "1")
    script.pip("install", "--no-index", installed)
    make_wheel(
        name="dep", version="1.0", metadata_updates={"Provides-Extra": "opt"}
    ).save_to_dir(script.scratch_path)
    roots = ["dep===1.0"]
    if source == "constraint":
        constraint = script.scratch_path / "constraints.txt"
        constraint.write_text("dep===1.0\n")
        roots = ["-c", str(constraint), "dep"]
    elif source == "dependency":
        create_basic_wheel_for_package(script, "app", "2", depends=["dep===1.0"])
        roots = ["app"]
    elif source == "extra":
        roots = ["dep[opt]===1.0"]
    if contextual:
        create_unfixed_url_dependency(script)
        roots.append("bridge")

    result = script.pip(
        "install",
        "-v",
        "--no-index",
        "--find-links",
        script.scratch_path,
        *roots,
    )

    if contextual:
        assert "Nab catalogue fallback" in result.stdout
    else:
        assert "Nab catalogue success" in result.stdout
        assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(dep="1.0")
    script.pip("check")


@pytest.mark.parametrize("allow_prerelease", [False, True])
def test_version_text_aliases_preserve_original_prerelease_admission(
    script: PipTestEnvironment, allow_prerelease: bool
) -> None:
    for version, build in [("1", "2"), ("1.0", "1")]:
        make_wheel(
            name="dep",
            version=version,
            metadata_updates={"Requires-Dist": "broken>=?"},
        ).save_to(script.scratch_path / f"dep-{version}-{build}-py3-none-any.whl")
    create_basic_wheel_for_package(script, "dep", "2rc1")

    result = script.pip(
        "install",
        "-v",
        "--no-index",
        "--find-links",
        script.scratch_path,
        *(["--pre"] if allow_prerelease else []),
        "dep",
        expect_error=not allow_prerelease,
        allow_stderr_warning=True,
    )

    if allow_prerelease:
        assert "Nab catalogue success" in result.stdout
        assert "Nab catalogue fallback" not in result.stdout
        script.assert_installed(dep="2rc1")
    else:
        assert (
            "Nab catalogue admission: dep has an ineligible prerelease" in result.stdout
        )
        script.assert_not_installed("dep")


def test_numeric_version_bounds_keep_existing_same_version_wheel_behavior(
    script: PipTestEnvironment,
) -> None:
    installed = create_basic_wheel_for_package(script, "dep", "1")
    script.pip("install", "--no-index", installed)
    alternative = create_basic_wheel_for_package(script, "dep", "1.0")

    script.pip("install", "--no-index", alternative, "dep==1.0")

    script.assert_installed(dep="1")
    script.pip("check")
