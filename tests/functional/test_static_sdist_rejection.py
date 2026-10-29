from pathlib import Path

import pytest

from tests.lib import PipTestEnvironment, create_basic_sdist_for_package


@pytest.mark.parametrize("command", ["download", "lock", "install", "report", "target"])
def test_reject_static_python_before_dynamic_dependencies(
    script: PipTestEnvironment, command: str
) -> None:
    for version, requires_python, dynamic in (
        ("1.0", ">=3.9", ""),
        (
            "2.0",
            ">=3.10" if command == "target" else ">=99",
            "Dynamic: Requires-Dist\n",
        ),
    ):
        create_basic_sdist_for_package(
            script,
            "python_limited",
            version,
            extra_files={
                "PKG-INFO": (
                    "Metadata-Version: 2.2\nName: python-limited\n"
                    f"Version: {version}\nRequires-Python: {requires_python}\n"
                    f"{dynamic}\n"
                ),
                "pyproject.toml": (
                    "[build-system]\nrequires = []\n"
                    'build-backend = "backend"\nbackend-path = ["."]\n'
                ),
                "backend.py": 'raise RuntimeError("unneeded backend executed")\n',
            },
        )

    actual_command = {"report": "install", "target": "download"}.get(command, command)
    args: list[str | Path] = [actual_command]
    args.extend(["--no-index", "--find-links", script.scratch_path])
    if command in {"install", "report"}:
        args.extend(["--dry-run", "--ignore-installed"])
    if command == "report":
        args.extend(["--report", "report.json"])
    if command == "target":
        args.extend(["--python-version", "3.9", "--no-deps"])
    result = script.pip(*args, "python-limited", allow_stderr_warning=command == "lock")
    if command in {"download", "target"}:
        assert "Successfully downloaded python-limited" in result.stdout
        assert "python_limited-1.0.tar.gz" in result.stdout
    elif command == "lock":
        lock = script.scratch_path / "pylock.toml"
        assert 'version = "1.0"' in lock.read_text()
        result.did_create(Path("scratch") / "pylock.toml")
    else:
        assert "Would install python-limited-1.0" in result.stdout


@pytest.mark.parametrize(
    "requires_python, dynamic_python, options",
    [
        (">=3.9", "", []),
        (">=99", "", ["--ignore-requires-python"]),
        (">=99", "Dynamic: Requires-Python\n", []),
    ],
)
def test_dynamic_dependencies_still_require_backend(
    script: PipTestEnvironment,
    requires_python: str,
    dynamic_python: str,
    options: list[str],
) -> None:
    archive = create_basic_sdist_for_package(
        script,
        "python_limited",
        "2.0",
        extra_files={
            "PKG-INFO": (
                "Metadata-Version: 2.2\nName: python-limited\nVersion: 2.0\n"
                f"Requires-Python: {requires_python}\n"
                f"Dynamic: Requires-Dist\n{dynamic_python}\n"
            ),
            "pyproject.toml": (
                "[build-system]\nrequires = []\n"
                'build-backend = "backend"\nbackend-path = ["."]\n'
            ),
            "backend.py": 'raise RuntimeError("required backend executed")\n',
        },
    )
    result = script.pip("download", "--no-index", *options, archive, expect_error=True)
    assert "required backend executed" in result.stderr
