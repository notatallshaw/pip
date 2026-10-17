import json
from pathlib import Path

import pytest

from tests.functional.test_nab_preparation_reuse import (
    preparation_count,
    tracked_install,
)
from tests.lib import PipTestEnvironment, create_basic_wheel_for_package
from tests.lib.wheel import make_wheel


def yanked_listing(
    script: PipTestEnvironment,
    packages: list[tuple[str, str, list[str]]],
    *,
    withdrawn: set[tuple[str, str]] | None = None,
) -> Path:
    """Build wheels and a local listing with the requested withdrawn files."""
    if withdrawn is None:
        withdrawn = {("dep", "2.0")}
    files = script.scratch_path / "files"
    files.mkdir()
    links = []
    for name, version, dependencies in packages:
        wheel = create_basic_wheel_for_package(
            script, name, version, depends=dependencies
        )
        wheel = wheel.rename(files / wheel.name)
        flag = ' data-yanked="withdrawn"' if (name, version) in withdrawn else ""
        links.append(f'<a href="files/{wheel.name}"{flag}>{wheel.name}</a>')
    path = script.scratch_path / "links.html"
    path.write_text("\n".join(links))
    return path


def test_unpinned_yank_does_not_force_a_restart(script: PipTestEnvironment) -> None:
    listing = yanked_listing(
        script,
        [
            ("dep", "1.0", ["missing==1"]),
            ("dep", "2.0", []),
            ("app", "2.0", ["dep>=1"]),
            ("app", "1.0", []),
        ],
    )
    result = script.pip(
        "install", "--ignore-installed", "--no-index", "--find-links", listing, "app"
    )
    assert "Nab catalogue success" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(app="1.0")
    script.assert_not_installed("dep")


def test_transitive_pin_keeps_the_parent_without_a_restart(
    script: PipTestEnvironment,
) -> None:
    listing = yanked_listing(
        script,
        [
            ("dep", "1.0", []),
            ("dep", "2.0", []),
            ("app", "2.0", ["dep==2.0"]),
            ("app", "1.0", ["dep==1.0"]),
        ],
    )
    result = script.pip(
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        listing,
        "app",
        allow_stderr_warning=True,
    )
    assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(app="2.0", dep="2.0")


def test_constraint_pin_keeps_the_yanked_release_available(
    script: PipTestEnvironment,
) -> None:
    listing = yanked_listing(script, [("dep", "1.0", []), ("dep", "2.0", [])])
    constraint = script.scratch_path / "constraints.txt"
    constraint.write_text("dep==2.0\n")
    result = script.pip(
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        listing,
        "--constraint",
        constraint,
        "dep",
        allow_stderr_warning=True,
    )
    assert "Nab catalogue success" in result.stdout
    script.assert_installed(dep="2.0")


def test_hidden_yanked_pin_is_found_without_a_restart(
    script: PipTestEnvironment,
) -> None:
    listing = yanked_listing(
        script,
        [("dep", "1.0", []), ("dep", "2.0", []), ("bridge", "1.0", ["dep==2.0"])],
    )
    result = script.pip(
        "install",
        "--ignore-installed",
        "--no-index",
        "--find-links",
        listing,
        "dep>=2",
        "bridge",
        allow_stderr_warning=True,
    )
    assert "Nab catalogue success" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(dep="2.0", bridge="1.0")


def test_rejected_parent_does_not_leave_yanked_eligibility(
    script: PipTestEnvironment,
) -> None:
    listing = yanked_listing(
        script,
        [
            ("dep", "1.0", []),
            ("dep", "2.0", []),
            ("app", "2.0", ["dep==2.0", "missing==1"]),
            ("app", "1.0", ["dep==1.0"]),
        ],
    )
    result = script.pip(
        "install", "--ignore-installed", "--no-index", "--find-links", listing, "app"
    )
    assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(app="1.0", dep="1.0")


def test_rejected_pin_cannot_admit_a_yanked_release_for_another_parent(
    script: PipTestEnvironment,
) -> None:
    listing = yanked_listing(
        script,
        [
            ("dep", "1.0", []),
            ("dep", "2.0", []),
            ("app", "2.0", ["dep==2.0", "missing==1"]),
            ("app", "1.0", ["dep>=2"]),
        ],
    )
    result = script.pip(
        "install", "--no-index", "--find-links", listing, "app", expect_error=True
    )
    assert "Nab catalogue fallback: IncompletePreferenceError" in result.stdout
    script.assert_not_installed("app", "dep")


@pytest.mark.parametrize(
    "declaration,allowed",
    [
        ("dep==2.0", True),
        ("dep===2.0", True),
        ("dep==2.*", False),
        ("dep>=2,<=2", False),
        ("dep>=2", False),
    ],
)
def test_only_exact_pins_admit_yanks_without_a_restart(
    script: PipTestEnvironment, declaration: str, allowed: bool
) -> None:
    listing = yanked_listing(script, [("dep", "2.0", [])])
    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        listing,
        declaration,
        expect_error=not allowed,
        allow_stderr_warning=True,
    )

    assert "Nab catalogue fallback" not in result.stdout
    if allowed:
        script.assert_installed(dep="2.0")
        assert "Reason for being yanked: withdrawn" in result.stderr
    else:
        script.assert_not_installed("dep")


@pytest.mark.parametrize("roots", [("dep>=2", "bridge"), ("bridge", "dep>=2")])
def test_hidden_pin_is_independent_of_input_order(
    script: PipTestEnvironment, roots: tuple[str, str]
) -> None:
    listing = yanked_listing(
        script, [("dep", "2.0", []), ("bridge", "1.0", ["dep==2.0"])]
    )
    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        listing,
        *roots,
        allow_stderr_warning=True,
    )

    assert "Nab catalogue success" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(dep="2.0", bridge="1.0")


@pytest.mark.parametrize(
    "dependency", ['dep==2.0 ; python_version < "2"', 'dep==2.0 ; extra == "unused"']
)
def test_inactive_dependency_pin_cannot_grant_permission(
    script: PipTestEnvironment, dependency: str
) -> None:
    listing = yanked_listing(
        script, [("dep", "2.0", []), ("app", "1.0", ["dep>=2", dependency])]
    )
    result = script.pip(
        "install", "--no-index", "--find-links", listing, "app", expect_error=True
    )

    assert "Nab catalogue fallback" not in result.stdout
    script.assert_not_installed("app", "dep")


def mixed_files_listing(
    script: PipTestEnvironment,
    *,
    live_dependency: str = "tools==1",
    withdrawn_dependency: str | None = "tools==2",
    extra_only: bool = False,
) -> Path:
    """Give one version live and withdrawn wheels with different dependencies."""
    links = []
    for build, dependency, withdrawn in [
        (1, live_dependency, False),
        (2, withdrawn_dependency, True),
    ]:
        filename = f"dep-1.0-{build}-py3-none-any.whl"
        requirement = (
            f'{dependency}; extra == "feature"'
            if extra_only and dependency is not None
            else dependency
        )
        make_wheel(
            name="dep",
            version="1.0",
            metadata_updates={
                "Provides-Extra": "feature",
                **({} if requirement is None else {"Requires-Dist": requirement}),
            },
        ).save_to(script.scratch_path / filename)
        flag = ' data-yanked="withdrawn"' if withdrawn else ""
        links.append(f'<a href="{filename}"{flag}>{filename}</a>')
    for version in ("1", "2"):
        wheel = create_basic_wheel_for_package(script, "tools", version)
        links.append(f'<a href="{wheel.name}">{wheel.name}</a>')
    listing = script.scratch_path / "links.html"
    listing.write_text("\n".join(links))
    return listing


@pytest.mark.parametrize(
    "roots",
    [("dep[feature]", "app", "tools==2"), ("app", "tools==2", "dep[feature]")],
)
@pytest.mark.parametrize("extra_version", [False, True])
def test_transitive_pin_admits_an_unversioned_extra(
    script: PipTestEnvironment, roots: tuple[str, ...], *, extra_version: bool
) -> None:
    """Keep the base facts when another parent permits an unversioned extra."""
    listing = mixed_files_listing(script, extra_only=True)
    app = create_basic_wheel_for_package(script, "app", "1", depends=["dep==1.0"])
    unrelated: Path | None = None
    if extra_version:
        unrelated = create_basic_wheel_for_package(
            script, "dep", "2.0", extras={"feature": []}
        )
    with listing.open("a") as stream:
        stream.write(f'\n<a href="{app.name}">{app.name}</a>')
        if unrelated is not None:
            stream.write(f'\n<a href="{unrelated.name}">{unrelated.name}</a>')
    report = script.scratch_path / "report.json"
    script.pip_expect_warning = True

    result = tracked_install(
        script, *roots, find_links=listing, options=("--report", str(report))
    )

    assert "NATIVE True" not in result.stdout
    assert "NATIVE False" not in result.stdout
    assert "Nab catalogue success" in result.stdout
    selected = next(
        item
        for item in json.loads(report.read_text())["install"]
        if item["metadata"]["name"] == "dep"
    )
    expected = script.scratch_path / "dep-1.0-2-py3-none-any.whl"
    assert selected["download_info"]["url"] == expected.as_uri()
    assert selected["is_yanked"]
    script.assert_installed(dep="1.0", tools="2", app="1")
    script.pip("check")
    if unrelated is not None:
        assert preparation_count(result.stdout, unrelated) == 0


@pytest.mark.parametrize("app_requirement", ["app==2", "app"])
def test_rejected_parent_metadata_cannot_expand_the_live_proof(
    script: PipTestEnvironment, app_requirement: str
) -> None:
    """Ignore an extra reached only through a known contradictory parent."""
    listing = mixed_files_listing(script)
    wheels = [
        create_basic_wheel_for_package(script, "app", "1", depends=["dep==1.0"]),
        create_basic_wheel_for_package(
            script,
            "app",
            "2",
            depends=["dep==1.0"],
            extras={"feature": ["ghost==1"]},
        ),
        create_basic_wheel_for_package(script, "parent", "1"),
        create_basic_wheel_for_package(
            script, "parent", "2", depends=["app[feature]==1"]
        ),
    ]
    with listing.open("a") as stream:
        for wheel in wheels:
            stream.write(f'\n<a href="{wheel.name}">{wheel.name}</a>')
    script.pip_expect_warning = True

    result = tracked_install(
        script, app_requirement, "parent", "tools==2", find_links=listing
    )

    assert "NATIVE True" not in result.stdout
    assert "NATIVE False" not in result.stdout
    assert "Nab catalogue success" in result.stdout
    script.assert_installed(app="2", parent="1", dep="1.0", tools="2")
    script.assert_not_installed("ghost")
    script.pip("check")


@pytest.mark.parametrize(
    "replacement", [False, True], ids=["missing-source", "replaced-source"]
)
@pytest.mark.parametrize("extra", [False, True], ids=["base", "extra"])
def test_unread_url_source_can_rescue_a_live_file(
    script: PipTestEnvironment, *, replacement: bool, extra: bool
) -> None:
    """A hidden URL can repair availability or replace same-version metadata."""
    listing = mixed_files_listing(
        script,
        live_dependency="support==1" if replacement else "support>=2",
        withdrawn_dependency=None,
    )
    hidden = script.scratch_path / "hidden"
    hidden.mkdir()
    name = "dep" if replacement else "support"
    direct = create_basic_wheel_for_package(
        script,
        name,
        "1.0" if replacement else "3",
        depends=["support==2"] if replacement else [],
        extras={"feature": []} if replacement else None,
    )
    direct = direct.rename(hidden / direct.name)
    bridge = Path(
        make_wheel(
            name="bridge",
            version="1",
            metadata=(
                "Metadata-Version: 2.1\nName: bridge\nVersion: 1\n"
                f"Requires-Dist: {name} @ {direct.as_uri()}\n"
            ),
        ).save_to_dir(script.scratch_path)
    )
    wheels = [
        create_basic_wheel_for_package(script, "bridge", "2"),
        bridge,
        create_basic_wheel_for_package(script, "support", "1"),
    ]
    if replacement:
        wheels.append(create_basic_wheel_for_package(script, "support", "2"))
    with listing.open("a") as stream:
        for wheel in wheels:
            stream.write(f'\n<a href="{wheel.name}">{wheel.name}</a>')
    report = script.scratch_path / "report.json"
    requirements = ["dep[feature]==1.0" if extra else "dep==1.0", "bridge"]
    if replacement:
        requirements.append("support==2")

    # Inspect the selected artifact even if a withdrawn file emitted a warning.
    script.pip_expect_warning = True
    result = tracked_install(
        script,
        *requirements,
        find_links=listing,
        options=("--report", str(report)),
    )

    assert "NATIVE True" not in result.stdout
    selected = next(
        item
        for item in json.loads(report.read_text())["install"]
        if item["metadata"]["name"] == "dep"
    )
    expected = (
        direct if replacement else script.scratch_path / "dep-1.0-1-py3-none-any.whl"
    )
    assert selected["download_info"]["url"] == expected.as_uri()
    assert not selected["is_yanked"]
    script.assert_installed(dep="1.0", bridge="1", support="2" if replacement else "3")
    script.pip("check")


@pytest.mark.parametrize("roots", [("dep==1.0", "tools"), ("tools", "dep==1.0")])
def test_live_file_can_downgrade_an_independently_requested_package(
    script: PipTestEnvironment, roots: tuple[str, str]
) -> None:
    listing = mixed_files_listing(script)
    result = script.pip("install", "--no-index", "--find-links", listing, *roots)

    assert "Nab catalogue success" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(dep="1.0", tools="1")
    assert "yanked version" not in result.stderr


def test_yank_can_resolve_after_live_dependencies_fail(
    script: PipTestEnvironment,
) -> None:
    listing = mixed_files_listing(script)
    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        listing,
        "dep==1.0",
        "tools==2",
        allow_stderr_warning=True,
    )

    assert "Nab catalogue success" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(dep="1.0", tools="2")
    assert "yanked version" in result.stderr


def test_yanked_candidate_cannot_supply_its_own_permission(
    script: PipTestEnvironment,
) -> None:
    listing = yanked_listing(script, [("dep", "2.0", ["dep==2.0"])])
    result = script.pip(
        "install", "--no-index", "--find-links", listing, "dep>=2", expect_error=True
    )

    assert "Nab catalogue fallback" not in result.stdout
    assert "Processing" not in result.stdout
    script.assert_not_installed("dep")


def test_exact_pin_on_an_extra_uses_the_same_withdrawn_base(
    script: PipTestEnvironment,
) -> None:
    listing = yanked_listing(script, [("dep", "2.0", [])])
    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        listing,
        "dep[feature]==2.0",
        allow_stderr_warning=True,
    )

    assert "Nab catalogue success" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(dep="2.0")


def test_yanking_still_checks_requires_python(script: PipTestEnvironment) -> None:
    wheel = script.scratch_path / "dep-2.0-py3-none-any.whl"
    make_wheel(
        name="dep", version="2.0", metadata_updates={"Requires-Python": ">=3"}
    ).save_to(wheel)
    listing = script.scratch_path / "links.html"
    listing.write_text(f'<a href="{wheel.name}" data-yanked="withdrawn">wheel</a>')
    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        listing,
        "dep==2.0",
        allow_stderr_warning=True,
    )

    assert "Nab catalogue success" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(dep="2.0")


def test_constraint_on_an_unrequested_package_does_not_grant_permission(
    script: PipTestEnvironment,
) -> None:
    listing = yanked_listing(script, [("dep", "2.0", []), ("app", "1", [])])
    constraint = script.scratch_path / "constraints.txt"
    constraint.write_text("dep==2.0\n")
    result = script.pip(
        "install", "--no-index", "--find-links", listing, "-c", constraint, "app"
    )

    assert "Nab catalogue success" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(app="1")
    script.assert_not_installed("dep")


def test_extra_can_use_a_yank_after_live_metadata_was_rejected(
    script: PipTestEnvironment,
) -> None:
    listing = mixed_files_listing(script)
    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        listing,
        "dep[feature]==1.0",
        "tools==2",
        allow_stderr_warning=True,
    )

    assert "Nab catalogue success" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(dep="1.0", tools="2")
    assert "yanked version" in result.stderr


@pytest.mark.parametrize("final_dependencies", [None, ["missing==1"]])
def test_a_yanked_final_does_not_suppress_live_prerelease_fallback(
    script: PipTestEnvironment, final_dependencies: list[str] | None
) -> None:
    packages: list[tuple[str, str, list[str]]] = [
        ("dep", "2.0", []),
        ("dep", "1.0a1", []),
    ]
    if final_dependencies is not None:
        packages.append(("dep", "1.0", final_dependencies))
    listing = yanked_listing(script, packages)
    result = script.pip("install", "--no-index", "--find-links", listing, "dep")

    assert "Nab catalogue success" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(dep="1.0a1")


def test_release_policy_can_exclude_live_prereleases(
    script: PipTestEnvironment,
) -> None:
    listing = yanked_listing(script, [("dep", "2.0", []), ("dep", "1.0a1", [])])
    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        listing,
        "--only-final=dep",
        "dep",
        expect_error=True,
    )

    assert "Nab catalogue fallback" not in result.stdout
    script.assert_not_installed("dep")


def test_yanking_preserves_a_satisfied_installed_prerelease(
    script: PipTestEnvironment,
) -> None:
    installed = create_basic_wheel_for_package(script, "dep", "1.1a1")
    script.pip("install", "--no-index", installed)
    listing = yanked_listing(
        script,
        [("dep", "1.0", []), ("app", "2.0", ["dep>=1"])],
        withdrawn={("app", "2.0")},
    )
    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        listing,
        "app==2.0",
        allow_stderr_warning=True,
    )

    assert "Nab catalogue success" in result.stdout
    assert "Nab catalogue fallback" not in result.stdout
    script.assert_installed(app="2.0", dep="1.1a1")
