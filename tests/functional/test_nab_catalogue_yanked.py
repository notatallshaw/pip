from pathlib import Path

from tests.lib import PipTestEnvironment, create_basic_wheel_for_package


def yanked_listing(
    script: PipTestEnvironment, packages: list[tuple[str, str, list[str]]]
) -> Path:
    """Build a local listing with a yanked dep2 and literal dependency metadata."""
    files = script.scratch_path / "files"
    files.mkdir()
    links = []
    for name, version, dependencies in packages:
        wheel = create_basic_wheel_for_package(
            script, name, version, depends=dependencies
        )
        wheel = wheel.rename(files / wheel.name)
        flag = ' data-yanked="withdrawn"' if name == "dep" and version == "2.0" else ""
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


def test_transitive_pin_retries_before_downgrading_the_parent(
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
    assert "yanked requirement pin" in result.stdout
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
    assert "yanked constraint pin" in result.stdout
    script.assert_installed(dep="2.0")


def test_hidden_yanked_pin_survives_an_unsuccessful_fast_attempt(
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
    assert "Nab catalogue fallback" in result.stdout
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
    assert "yanked requirement pin" in result.stdout
    script.assert_installed(app="1.0", dep="1.0")
