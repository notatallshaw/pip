import hashlib
from pathlib import Path

import pytest

from tests.lib import (
    PipTestEnvironment,
    create_basic_sdist_for_package,
    create_basic_wheel_for_package,
)
from tests.lib.wheel import make_wheel


def test_known_dependency_url_uses_fixed_input_source(
    script: PipTestEnvironment,
) -> None:
    dep = create_basic_wheel_for_package(script, "dep", "1")
    make_wheel(
        name="app",
        version="1",
        metadata=(
            "Metadata-Version: 2.2\nName: app\nVersion: 1\n"
            f"Requires-Dist: dep @ {dep.as_uri()}\n"
        ),
    ).save_to_dir(script.scratch_path)

    result = script.pip(
        "install", "--no-index", "--find-links", script.scratch_path, "app", dep
    )

    assert "Nab catalogue success" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(app="1", dep="1")


@pytest.mark.parametrize("active", [False, True])
def test_source_constraint_is_prepared_only_when_required(
    script: PipTestEnvironment, active: bool
) -> None:
    app = create_basic_wheel_for_package(script, "app", "1")
    create_basic_wheel_for_package(script, "app", "2")
    create_basic_wheel_for_package(script, "other", "1")
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"app @ {app.as_uri()}\n")
    if not active:
        app.unlink()

    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "-c",
        constraints,
        "app" if active else "other",
    )

    assert "Nab catalogue success" in result.stdout
    script.assert_installed(**({"app": "1"} if active else {"other": "1"}))


@pytest.mark.parametrize("valid", [False, True])
def test_fixed_hash_policy_is_verified_on_fast_path(
    script: PipTestEnvironment, valid: bool
) -> None:
    wheel = create_basic_wheel_for_package(script, "app", "1")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest() if valid else "0" * 64
    requirements = script.scratch_path / "requirements.txt"
    requirements.write_text(f"app==1 --hash=sha256:{digest}\n")

    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "-r",
        requirements,
        expect_error=not valid,
    )

    assert "Nab catalogue fallback" not in result.stdout
    if valid:
        assert "Nab catalogue success" in result.stdout
        script.assert_installed(app="1")
    else:
        assert "DO NOT MATCH THE HASHES" in result.stderr
        script.assert_not_installed("app")


def test_conflicting_input_sources_fail_without_native_solve(
    script: PipTestEnvironment,
) -> None:
    first = create_basic_wheel_for_package(script, "app", "1")
    alternate = script.scratch_path / "alternate"
    alternate.mkdir()
    second = alternate / first.name
    second.write_bytes(first.read_bytes())

    result = script.pip("install", "--no-index", first, second, expect_error=True)

    assert "ResolutionImpossible" in result.stderr
    assert "Nab catalogue fallback" not in result.stdout


def test_contradictory_root_ranges_fail_without_native_solve(
    script: PipTestEnvironment,
) -> None:
    result = script.pip("install", "--no-index", "app<1", "app>=2", expect_error=True)

    assert "ResolutionImpossible" in result.stderr
    assert "Nab catalogue fallback" not in result.stdout


def test_complete_dependency_domain_certifies_failure(
    script: PipTestEnvironment,
) -> None:
    create_basic_wheel_for_package(script, "app", "1", depends=["missing==1"])

    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "app",
        expect_error=True,
    )

    assert "Nab catalogue certified failure" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    assert "No matching distribution found for missing==1" in result.stderr


def test_fixed_url_conflict_reports_both_requirements(
    script: PipTestEnvironment,
) -> None:
    direct = create_basic_wheel_for_package(script, "dep", "1")
    alternate = script.scratch_path / "alternate"
    alternate.mkdir()
    other = alternate / direct.name
    other.write_bytes(direct.read_bytes())
    parent = script.scratch_path / "app-1-py3-none-any.whl"
    make_wheel(
        name="app",
        version="1",
        metadata=(
            "Metadata-Version: 2.2\nName: app\nVersion: 1\n"
            f"Requires-Dist: dep @ {other.as_uri()}\n"
        ),
    ).save_to(parent)

    result = script.pip("install", "--no-index", parent, direct, expect_error=True)

    assert "ResolutionImpossible" in result.stderr
    assert "app 1 depends on dep" in result.stdout
    assert str(direct) in result.stdout
    assert str(other) in result.stdout
    assert "Nab catalogue fallback" not in result.stdout


@pytest.mark.parametrize(
    "invalid_field,expected", [("Version", "2.0"), ("Requires-Dist", "1.0")]
)
def test_metadata_rejection_scope_preserves_artifact_alternatives(
    script: PipTestEnvironment, invalid_field: str, expected: str
) -> None:
    create_basic_wheel_for_package(script, "pkg", "1.0")
    create_basic_sdist_for_package(script, "pkg", "2.0")
    rejected = script.scratch_path / "pkg-2.0-py2.py3-none-any.whl"
    make_wheel(
        name="pkg",
        version="2.0",
        metadata_updates={
            invalid_field: "9.0" if invalid_field == "Version" else "broken>=?"
        },
    ).save_to(rejected)

    result = script.pip(
        "install",
        "--no-index",
        "--no-build-isolation",
        "--find-links",
        script.scratch_path,
        "pkg",
        allow_stderr_warning=True,
    )

    assert "Nab catalogue success" in result.stdout
    script.assert_installed(pkg=expected)


def installed_metadata_listing(
    script: PipTestEnvironment, *, yanked: bool, reject_all: bool = False
) -> Path:
    """Install app1 and offer index metadata errors in either catalogue mode."""
    installed = create_basic_wheel_for_package(script, "app", "1")
    script.pip("install", "--no-index", installed)

    valid = create_basic_wheel_for_package(script, "app", "2")
    rejected = script.scratch_path / "app-3-py3-none-any.whl"
    make_wheel(
        name="app", version="3", metadata_updates={"Requires-Dist": "broken>=?"}
    ).save_to(rejected)
    if reject_all:
        make_wheel(
            name="app", version="2", metadata_updates={"Requires-Dist": "broken>=?"}
        ).save_to(valid)
    if not yanked:
        return script.scratch_path

    withdrawn = create_basic_wheel_for_package(script, "app", "4")
    listing = script.scratch_path / "links.html"
    listing.write_text(
        f'<a href="{valid.name}">{valid.name}</a>\n'
        f'<a href="{rejected.name}">{rejected.name}</a>\n'
        f'<a href="{withdrawn.name}" data-yanked="withdrawn">{withdrawn.name}</a>\n'
    )
    return listing


@pytest.mark.parametrize("yanked", [False, True])
@pytest.mark.parametrize("bound", ["root", "constraint", "extras", "extra-base"])
def test_invalid_index_metadata_stays_fast_when_inputs_exclude_installation(
    script: PipTestEnvironment, bound: str, yanked: bool
) -> None:
    listing = installed_metadata_listing(script, yanked=yanked)
    arguments: list[str | Path]
    if bound == "constraint":
        constraint = script.scratch_path / "constraints.txt"
        constraint.write_text("app>=2,<4\n")
        arguments = ["--constraint", constraint, "app"]
    elif bound == "extra-base":
        arguments = ["app[feature]", "app>=2,<4"]
    else:
        arguments = ["app[feature]>=2,<4" if bound == "extras" else "app>=2,<4"]

    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        listing,
        *arguments,
        allow_stderr_warning=True,
    )

    assert "Nab catalogue success" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    assert (
        result.stderr.count("Ignoring version 3 of app since it has invalid metadata")
        == 1
    )
    script.assert_installed(app="2")


@pytest.mark.parametrize("yanked", [False, True])
def test_invalid_index_metadata_does_not_make_installed_upgrades_fail(
    script: PipTestEnvironment, yanked: bool
) -> None:
    listing = installed_metadata_listing(script, yanked=yanked)
    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        listing,
        "--upgrade",
        "app",
        allow_stderr_warning=True,
    )

    assert "Nab catalogue success" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    assert "Ignoring version 3 of app since it has invalid metadata" in result.stderr
    script.assert_installed(app="2")


def test_dependency_exclusion_rejects_invalid_metadata_without_fallback(
    script: PipTestEnvironment,
) -> None:
    listing = installed_metadata_listing(script, yanked=False)
    create_basic_wheel_for_package(script, "bridge", "1", depends=["app>=2,<4"])
    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        listing,
        "app",
        "bridge",
        allow_stderr_warning=True,
    )

    assert "Nab catalogue success" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(app="2", bridge="1")


@pytest.mark.parametrize("yanked", [False, True])
def test_excluded_installation_does_not_repeat_metadata_rejections_on_failure(
    script: PipTestEnvironment, yanked: bool
) -> None:
    listing = installed_metadata_listing(script, yanked=yanked, reject_all=True)
    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        listing,
        "app>=2,<4",
        expect_error=True,
    )

    assert "Nab catalogue fallback" not in result.stdout
    for version in ("2", "3"):
        assert (
            result.stderr.count(
                f"Ignoring version {version} of app since it has invalid metadata"
            )
            == 1
        )
    script.assert_installed(app="1")


def test_excluded_installation_does_not_certify_an_unread_url_source(
    script: PipTestEnvironment,
) -> None:
    installed = create_basic_wheel_for_package(script, "dep", "1")
    script.pip("install", "--no-index", installed)
    index = script.scratch_path / "index"
    index.mkdir()
    supplied = create_basic_wheel_for_package(script, "dep", "3")
    make_wheel(
        name="bridge",
        version="1",
        metadata=(
            "Metadata-Version: 2.2\nName: bridge\nVersion: 1\n"
            f"Requires-Dist: dep @ {supplied.as_uri()}\n"
        ),
    ).save_to(index / "bridge-1-py3-none-any.whl")

    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        index,
        "dep>=3",
        "bridge",
    )

    assert "Nab catalogue fallback: ResolutionError" in result.stdout
    assert "Nab catalogue certified failure" not in result.stdout
    script.assert_installed(dep="3", bridge="1")


@pytest.mark.parametrize("required", [False, True])
def test_rejected_source_constraint_does_not_restart_or_use_the_index(
    script: PipTestEnvironment, required: bool
) -> None:
    direct = script.scratch_path / "direct"
    direct.mkdir()
    rejected = direct / "dep-1-py3-none-any.whl"
    make_wheel(name="dep", version="1", metadata_updates={"Name": "other"}).save_to(
        rejected
    )
    create_basic_wheel_for_package(script, "dep", "2")
    create_basic_wheel_for_package(script, "app", "2", depends=["dep"])
    create_basic_wheel_for_package(script, "app", "1")
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"dep @ {rejected.as_uri()}\n")

    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--constraint",
        constraints,
        "dep" if required else "app",
        expect_error=required,
    )

    assert "Nab catalogue fallback" not in result.stdout
    script.assert_not_installed("dep")
    if required:
        assert "ResolutionImpossible" in result.stderr
    else:
        assert "Nab catalogue success" in result.stdout
        script.assert_installed(app="1")


@pytest.mark.parametrize("conditional", [False, True])
def test_invalid_constrained_source_metadata_errors_without_a_restart(
    script: PipTestEnvironment, conditional: bool
) -> None:
    direct = script.scratch_path / "direct"
    direct.mkdir()
    rejected = direct / "dep-1-py3-none-any.whl"
    make_wheel(
        name="dep", version="1", metadata_updates={"Requires-Dist": "broken>=?"}
    ).save_to(rejected)
    create_basic_wheel_for_package(script, "dep", "2")
    create_basic_wheel_for_package(script, "app", "2", depends=["dep"])
    create_basic_wheel_for_package(script, "app", "1")
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"dep @ {rejected.as_uri()}\n")

    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--constraint",
        constraints,
        "app" if conditional else "dep",
        expect_error=True,
    )

    assert "Nab catalogue fallback" not in result.stdout
    assert "has invalid metadata" in result.stderr
    assert (
        result.stdout.replace("\\", "/").count(f"Processing ./direct/{rejected.name}")
        == 1
    )
    script.assert_not_installed("app", "dep")


def test_invalid_metadata_in_mandatory_url_closure_errors_without_a_restart(
    script: PipTestEnvironment,
) -> None:
    dep = script.scratch_path / "dep-1-py3-none-any.whl"
    make_wheel(
        name="dep", version="1", metadata_updates={"Requires-Dist": "broken>=?"}
    ).save_to(dep)
    app = script.scratch_path / "app-1-py3-none-any.whl"
    make_wheel(
        name="app",
        version="1",
        metadata=(
            "Metadata-Version: 2.2\nName: app\nVersion: 1\n"
            f"Requires-Dist: dep @ {dep.as_uri()}\n"
        ),
    ).save_to(app)

    result = script.pip("install", "--no-index", app, expect_error=True)

    assert "Nab catalogue fallback" not in result.stdout
    assert "has invalid metadata" in result.stderr
    assert result.stdout.replace("\\", "/").count(f"Processing ./{dep.name}") == 1
    script.assert_not_installed("app", "dep")


@pytest.mark.parametrize("upgrade", [False, True])
def test_contextual_installed_iteration_skips_invalid_index_metadata(
    script: PipTestEnvironment, upgrade: bool
) -> None:
    installed = create_basic_wheel_for_package(script, "app", "1", depends=["other==1"])
    script.pip("install", "--no-index", "--no-deps", installed)
    create_basic_wheel_for_package(script, "app", "2")
    make_wheel(
        name="app", version="3", metadata_updates={"Requires-Dist": "broken>=?"}
    ).save_to(script.scratch_path / "app-3-py3-none-any.whl")
    create_basic_wheel_for_package(script, "other", "2")
    direct = script.scratch_path / "direct"
    direct.mkdir()
    supplied = create_basic_wheel_for_package(script, "dep", "1")
    supplied = supplied.rename(direct / supplied.name)
    make_wheel(
        name="bridge",
        version="1",
        metadata=(
            "Metadata-Version: 2.2\nName: bridge\nVersion: 1\n"
            f"Requires-Dist: dep @ {supplied.as_uri()}\n"
        ),
    ).save_to(script.scratch_path / "bridge-1-py3-none-any.whl")

    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        *(["--upgrade"] if upgrade else []),
        "app",
        "other==2",
        "bridge",
        allow_stderr_warning=True,
    )

    assert (
        "Nab catalogue fallback: CatalogueUnsupported: URL dependency" in result.stdout
    )
    assert "Ignoring version 3 of app since it has invalid metadata" in result.stderr
    script.assert_installed(app="2", other="2", bridge="1", dep="1")


@pytest.mark.parametrize("alternative", ["absent", "same", "newer"])
def test_read_installed_metadata_can_certify_a_closed_failed_domain(
    script: PipTestEnvironment,
    alternative: str,
) -> None:
    initial = create_basic_wheel_for_package(script, "dep", "1", depends=["missing==1"])
    script.pip("install", "--no-index", "--no-deps", initial)
    index = script.scratch_path / "index"
    index.mkdir()
    if alternative != "absent":
        version = "1" if alternative == "same" else "2"
        wheel = make_wheel(
            name="dep",
            version=version,
            metadata_updates={"Requires-Dist": "missing==2"} if version == "2" else {},
        )
        wheel.save_to(index / f"dep-{version}-py3-none-any.whl")

    result = script.pip(
        "install",
        "-v",
        "--no-index",
        "--find-links",
        index,
        "dep",
        expect_error=True,
    )

    assert "Nab catalogue certified failure" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(dep="1")


def test_installed_metadata_does_not_cover_an_unread_version_text_source(
    script: PipTestEnvironment,
) -> None:
    initial = create_basic_wheel_for_package(script, "dep", "1", depends=["missing==1"])
    script.pip("install", "--no-index", "--no-deps", initial)
    index = script.scratch_path / "index"
    index.mkdir()
    make_wheel(name="dep", version="1.0").save_to(index / "dep-1.0-py3-none-any.whl")
    other = create_basic_wheel_for_package(script, "other", "2", depends=["dep===1.0"])

    result = script.pip(
        "install",
        "-v",
        "--no-index",
        "--find-links",
        index,
        "dep",
        other,
    )

    assert "Nab catalogue fallback" in result.stdout
    assert "Nab catalogue certified failure" not in result.stdout
    script.assert_installed(dep="1.0", other="2")
    script.pip("check")


def test_no_deps_can_certify_failure_without_reading_possible_url_dependencies(
    script: PipTestEnvironment,
) -> None:
    index = script.scratch_path / "index"
    index.mkdir()
    supplied = create_basic_wheel_for_package(script, "dep", "3")
    make_wheel(name="dep", version="1").save_to(index / "dep-1-py3-none-any.whl")
    make_wheel(
        name="bridge",
        version="1",
        metadata=(
            "Metadata-Version: 2.2\nName: bridge\nVersion: 1\n"
            f"Requires-Dist: dep @ {supplied.as_uri()}\n"
        ),
    ).save_to(index / "bridge-1-py3-none-any.whl")

    result = script.pip(
        "install",
        "-v",
        "--no-index",
        "--no-deps",
        "--find-links",
        index,
        "dep>=3",
        "bridge",
        expect_error=True,
    )

    assert "Nab catalogue certified failure" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    assert "No matching distribution found for dep>=3" in result.stderr
    script.assert_not_installed("dep", "bridge")


@pytest.mark.parametrize("rejected", [False, True])
def test_impossible_mandatory_source_does_not_read_an_unrelated_url_parent(
    script: PipTestEnvironment,
    rejected: bool,
) -> None:
    fixed = script.scratch_path / "fixed"
    fixed.mkdir()
    constrained = fixed / "dep-1-py3-none-any.whl"
    make_wheel(
        name="dep",
        version="1",
        metadata_updates={"Name": "other"} if rejected else {},
    ).save_to(constrained)
    index = script.scratch_path / "index"
    index.mkdir()
    supplied = create_basic_wheel_for_package(script, "dep", "3")
    make_wheel(
        name="bridge",
        version="1",
        metadata=(
            "Metadata-Version: 2.2\nName: bridge\nVersion: 1\n"
            f"Requires-Dist: dep @ {supplied.as_uri()}\n"
        ),
    ).save_to(index / "bridge-1-py3-none-any.whl")
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"dep @ {constrained.as_uri()}\n")

    result = script.pip(
        "install",
        "-v",
        "--no-index",
        "--find-links",
        index,
        "-c",
        constraints,
        "dep>=3",
        "bridge",
        expect_error=True,
    )

    assert "Nab catalogue certified failure" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    assert "Nab native retry" not in result.stdout
    assert "bridge-1-py3-none-any.whl" not in result.stdout
    assert "ResolutionImpossible" in result.stderr
    script.assert_not_installed("dep", "bridge")
