import hashlib
import json
from pathlib import Path

import pytest

from tests.lib import PipTestEnvironment, create_basic_wheel_for_package
from tests.lib.resolver import (
    preparation_count,
    tracked_install,
    yanked_listing,
)
from tests.lib.wheel import make_wheel


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
    # The first file at dep 2 cannot satisfy its own dependency.
    live = listing.parent / "files" / "dep-2.0-1-py3-none-any.whl"
    make_wheel("dep", "2.0", metadata_updates={"Requires-Dist": "missing"}).save_to(
        live
    )
    with listing.open("a") as page:
        page.write(f'\n<a href="{live.as_uri()}">{live.name}</a>')

    result = script.pip_install_local(
        "--ignore-installed", "app", find_links=listing, allow_stderr_warning=True
    )
    assert "Nab source-domain restart" not in result.stdout
    assert "yanked version" in result.stderr
    script.assert_installed(app="2.0", dep="2.0")


def test_constraint_pin_keeps_the_yanked_release_available(
    script: PipTestEnvironment,
) -> None:
    listing = yanked_listing(script, [("dep", "1.0", []), ("dep", "2.0", [])])
    constraint = script.scratch_path / "constraints.txt"
    constraint.write_text("dep==2.0\n")
    report = script.pip_install_local_report(
        "--constraint", constraint, "dep", find_links=listing, allow_stderr_warning=True
    )

    (selected,) = report["install"]
    assert selected["metadata"]["version"] == "2.0"
    assert selected["is_yanked"]


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
    result = script.pip_install_local("app", find_links=listing, expect_error=True)
    assert result.returncode == 1
    assert "missing==1" in result.stderr
    assert "No matching distribution found" in result.stderr
    assert "Traceback" not in result.stderr
    script.assert_not_installed("app", "dep")


@pytest.mark.parametrize(
    "declaration,allowed",
    [
        ("dep==2.0", True),
        ("dep>=2,<=2", False),
    ],
)
def test_only_exact_pins_admit_yanks_without_a_restart(
    script: PipTestEnvironment, declaration: str, allowed: bool
) -> None:
    listing = yanked_listing(script, [("dep", "2.0", [])])
    result = script.pip_install_local(
        declaration,
        find_links=listing,
        expect_error=not allowed,
        allow_stderr_warning=True,
    )

    assert "Nab source-domain restart" not in result.stdout
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
    script.pip_install_local(*roots, find_links=listing, allow_stderr_warning=True)

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
    script.pip_install_local("app", find_links=listing, expect_error=True)

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

    script.pip_install_local(
        "--ignore-installed", app_requirement, "parent", "tools==2", find_links=listing
    )

    script.assert_installed(app="2", parent="1", dep="1.0", tools="2")
    script.assert_not_installed("ghost")
    script.pip("check")


@pytest.mark.parametrize(
    "replacement", [False, True], ids=["missing-source", "replaced-source"]
)
@pytest.mark.parametrize("extra", [False, True], ids=["base", "extra"])
@pytest.mark.parametrize(
    "upfront", [False, True], ids=["unread-source", "input-source"]
)
def test_live_file_preference_uses_known_sources(
    script: PipTestEnvironment, *, replacement: bool, extra: bool, upfront: bool
) -> None:
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
            metadata_updates={"Requires-Dist": f"{name} @ {direct.as_uri()}"},
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
    if upfront:
        requirements.append(f"{name} @ {direct.as_uri()}")

    # The exact dep pin permits the withdrawn artifact in the known source domain.
    script.pip_expect_warning = True
    result = tracked_install(
        script,
        *requirements,
        find_links=listing,
        options=("--report", str(report)),
    )

    assert "Nab source-domain restart" not in result.stdout
    reads = [
        line
        for line in result.stdout.splitlines()
        if line.startswith("DEPENDENCY_READ ")
    ]
    assert not any(direct.as_uri() in line for line in reads)
    selected = {
        item["metadata"]["name"]: item
        for item in json.loads(report.read_text())["install"]
    }
    expected_dep = (
        (direct if replacement else script.scratch_path / "dep-1.0-1-py3-none-any.whl")
        if upfront
        else script.scratch_path / "dep-1.0-2-py3-none-any.whl"
    )
    assert selected["dep"]["download_info"]["url"] == expected_dep.as_uri()
    assert selected["dep"]["is_yanked"] is not upfront
    assert selected["bridge"]["metadata"]["version"] == "2"
    assert (
        selected["dep"]["download_info"]["archive_info"]["hashes"]["sha256"]
        == hashlib.sha256(expected_dep.read_bytes()).hexdigest()
    )
    if replacement or upfront:
        script.assert_installed(
            dep="1.0", bridge="2", support="2" if replacement else "3"
        )
    else:
        script.assert_installed(dep="1.0", bridge="2")
        script.assert_not_installed("support")
    script.pip("check")


@pytest.mark.parametrize("roots", [("dep==1.0", "tools"), ("tools", "dep==1.0")])
def test_live_file_can_downgrade_an_independently_requested_package(
    script: PipTestEnvironment, roots: tuple[str, str]
) -> None:
    listing = mixed_files_listing(script)
    result = script.pip_install_local(*roots, find_links=listing)

    script.assert_installed(dep="1.0", tools="1")
    assert "yanked version" not in result.stderr


def test_yank_can_resolve_after_live_dependencies_fail(
    script: PipTestEnvironment,
) -> None:
    listing = mixed_files_listing(script)
    result = script.pip_install_local(
        "dep==1.0", "tools==2", find_links=listing, allow_stderr_warning=True
    )

    script.assert_installed(dep="1.0", tools="2")
    assert "yanked version" in result.stderr


def test_exact_pin_on_an_extra_uses_the_same_withdrawn_base(
    script: PipTestEnvironment,
) -> None:
    listing = yanked_listing(script, [("dep", "2.0", [])])
    script.pip_install_local(
        "dep[feature]==2.0", find_links=listing, allow_stderr_warning=True
    )

    script.assert_installed(dep="2.0")


def test_yanked_pin_rejects_an_incompatible_python(script: PipTestEnvironment) -> None:
    wheel = script.scratch_path / "dep-2.0-py3-none-any.whl"
    make_wheel(
        name="dep", version="2.0", metadata_updates={"Requires-Python": ">=100"}
    ).save_to(wheel)
    listing = script.scratch_path / "links.html"
    listing.write_text(f'<a href="{wheel.name}" data-yanked="withdrawn">wheel</a>')
    result = script.pip_install_local("dep==2.0", find_links=listing, expect_error=True)

    assert ">=100" in result.stderr


def test_constraint_on_an_unrequested_package_does_not_grant_permission(
    script: PipTestEnvironment,
) -> None:
    listing = yanked_listing(script, [("dep", "2.0", []), ("app", "1", [])])
    constraint = script.scratch_path / "constraints.txt"
    constraint.write_text("dep==2.0\n")
    report = script.pip_install_local_report(
        "-c", constraint, "app", find_links=listing
    )

    assert [item["metadata"]["name"] for item in report["install"]] == ["app"]


def test_extra_can_use_a_yank_after_live_metadata_was_rejected(
    script: PipTestEnvironment,
) -> None:
    listing = mixed_files_listing(script)
    result = script.pip_install_local(
        "dep[feature]==1.0", "tools==2", find_links=listing, allow_stderr_warning=True
    )

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
    result = script.pip_install_local("dep", find_links=listing)

    assert "Nab source-domain restart" not in result.stdout
    script.assert_installed(dep="1.0a1")


def test_release_policy_can_exclude_live_prereleases(
    script: PipTestEnvironment,
) -> None:
    listing = yanked_listing(script, [("dep", "2.0", []), ("dep", "1.0a1", [])])
    script.pip_install_local(
        "--only-final=dep", "dep", find_links=listing, expect_error=True
    )

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
    script.pip_install_local("app==2.0", find_links=listing, allow_stderr_warning=True)

    script.assert_installed(app="2.0", dep="1.1a1")
