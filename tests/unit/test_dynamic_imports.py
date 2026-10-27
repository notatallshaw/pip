import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "kind", ["command", "vendor", "resource", "lexer", "formatter", "style", "filter"]
)
def test_dynamic_imports_respect_install_guard(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real entry points exercise the delegated import in EntryPoint.load().
    metadata = tmp_path / "guarded_import_plugin-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: guarded-import-plugin\nVersion: 1.0\n"
    )
    (metadata / "entry_points.txt").write_text(
        "".join(
            f"[pygments.{group}]\nguarded = guarded_import_plugin:VALUE\n"
            for group in ("lexers", "formatters", "styles", "filters")
        )
    )
    (tmp_path / "guarded_import_plugin.py").write_text("VALUE = 42\n")
    resource_package = tmp_path / "guarded_import_resource"
    resource_package.mkdir()
    (resource_package / "__init__.py").write_text("VALUE = 42\n")
    (resource_package / "resource.txt").write_text("resource contents")
    python_path = os.pathsep.join((str(tmp_path), os.environ.get("PYTHONPATH", "")))
    monkeypatch.setenv("PYTHONPATH", python_path)
    script = (
        Path(__file__).resolve().parents[1] / "lib" / "check_installation_imports.py"
    )
    subprocess.run([sys.executable, str(script), kind], check=True)
