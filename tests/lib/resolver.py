"""Build local resolver inputs and observe the files pip actually prepares."""

from pathlib import Path

from tests.lib import PipTestEnvironment, TestPipResult, create_basic_wheel_for_package

PREPARATION_RUNNER = Path(__file__).with_name("install_with_preparation_counts.py")


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


def tracked_install(
    script: PipTestEnvironment,
    *requirements: str | Path,
    options: tuple[str, ...] = (),
    expect_error: bool = False,
    ignore_installed: bool = True,
    find_links: Path | None = None,
) -> TestPipResult:
    """Run real pip while counting candidate preparation at its native boundary."""
    return script.run(
        "python",
        str(PREPARATION_RUNNER),
        "install",
        *(("--ignore-installed",) if ignore_installed else ()),
        "--no-index",
        "--find-links",
        str(script.scratch_path if find_links is None else find_links),
        *options,
        *(str(requirement) for requirement in requirements),
        expect_error=expect_error,
        allow_stderr_warning=True if script.pip_expect_warning else None,
    )


def preparation_count(output: str, wheel: Path) -> int:
    """Count calls to the real LinkCandidate preparation method for one wheel."""
    return output.splitlines().count("PREPARE " + wheel.name)
