"""Tests for --max-resolution-rounds option."""

from tests.lib import PipTestEnvironment


def test_max_resolution_rounds_option(script: PipTestEnvironment) -> None:
    """Test that the --max-resolution-rounds option is recognized and used."""
    # Create a minimal package with no dependencies
    pkg_path = script.scratch_path / "pkg"
    pkg_path.mkdir()
    script.scratch_path.joinpath("pkg/pyproject.toml").write_text(
        """[build-system]
requires = ["setuptools>=42", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "pkg"
version = "0.1"
description = "A test package"
requires-python = ">=3.8"
""",
        encoding="utf-8",
    )

    # Install with a low value of max-resolution-rounds, should still succeed
    # since there are no dependencies
    result = script.pip("install", "--max-resolution-rounds=10", pkg_path)
    assert "Successfully installed pkg-0.1" in result.stdout

    # Uninstall the package
    script.pip("uninstall", "-y", "pkg")

    # Create a fake dependency A
    dep_a_path = script.scratch_path / "dep_a"
    dep_a_path.mkdir()
    script.scratch_path.joinpath("dep_a/pyproject.toml").write_text(
        """[build-system]
requires = ["setuptools>=42", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "dep-a"
version = "0.1"
description = "Dependency A"
requires-python = ">=3.8"
dependencies = ["dep-b>=0.1"]
""",
        encoding="utf-8",
    )

    # Create a fake dependency B that depends on A
    dep_b_path = script.scratch_path / "dep_b"
    dep_b_path.mkdir()
    script.scratch_path.joinpath("dep_b/pyproject.toml").write_text(
        """[build-system]
requires = ["setuptools>=42", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "dep-b"
version = "0.1"
description = "Dependency B"
requires-python = ">=3.8"
dependencies = ["dep-a>=0.1"]
""",
        encoding="utf-8",
    )

    # Create a main package that depends on both A and B
    complex_pkg_path = script.scratch_path / "complex_pkg"
    complex_pkg_path.mkdir()
    script.scratch_path.joinpath("complex_pkg/pyproject.toml").write_text(
        """[build-system]
requires = ["setuptools>=42", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "complex-pkg"
version = "0.1"
description = "A package with circular dependencies"
requires-python = ">=3.8"
dependencies = ["dep-a>=0.1", "dep-b>=0.1"]
""",
        encoding="utf-8",
    )

    # Install with an extremely low max-resolution-rounds to force failure
    result = script.pip(
        "install",
        "--max-resolution-rounds=1",
        complex_pkg_path,
        expect_error=True,
    )

    assert result.returncode != 0
