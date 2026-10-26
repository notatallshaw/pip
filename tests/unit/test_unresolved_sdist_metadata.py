import sys
from pathlib import Path

import pytest

from pip._internal.metadata import get_metadata_distribution
from pip._internal.req.constructors import install_req_from_line


def test_actual_distribution_replaces_unresolved_source_metadata() -> None:
    req = install_req_from_line("demo")
    partial = get_metadata_distribution(
        b"Metadata-Version: 2.2\nName: demo\nVersion: 1\nDynamic: Provides-Extra\n",
        "demo-1.tar.gz",
        "demo",
    )
    req.set_dist(partial)
    req.unresolved_metadata_fields = frozenset({"provides-extra"})
    assert req.metadata.get_all("Provides-Extra") is None
    actual = get_metadata_distribution(
        b"Metadata-Version: 2.2\nName: demo\nVersion: 1\nDynamic: Provides-Extra\n"
        b"Provides-Extra: speed\n",
        "demo-1-py3-none-any.whl",
        "demo",
    )

    req.set_dist(actual)

    assert req.unresolved_metadata_fields == frozenset()
    assert list(req.get_dist().iter_provided_extras()) == ["speed"]
    assert req.metadata.get_all("Provides-Extra") == ["speed"]
    assert req.get_dist().metadata.get_all("Dynamic") == ["Provides-Extra"]


def test_backend_metadata_completes_unresolved_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\nbuild-backend = "unused_backend"\n'
    )
    info = tmp_path / "demo-1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        "Metadata-Version: 2.2\nName: demo\nVersion: 1\n"
        "Dynamic: Provides-Extra\nProvides-Extra: speed\n"
    )
    req = install_req_from_line("demo")
    req.source_dir = str(tmp_path)
    req.load_pyproject_toml()
    req.configure_backend(sys.executable)
    req.set_dist(
        get_metadata_distribution(
            b"Metadata-Version: 2.2\nName: demo\nVersion: 1\nDynamic: Provides-Extra\n",
            "demo-1.tar.gz",
            "demo",
        )
    )
    req.unresolved_metadata_fields = frozenset({"provides-extra"})
    assert req.metadata.get_all("Provides-Extra") is None
    monkeypatch.setattr(
        "pip._internal.req.req_install.generate_metadata", lambda **_: str(info)
    )

    req.prepare_metadata(allow_editables=False)

    assert req.unresolved_metadata_fields == frozenset()
    assert list(req.get_dist().iter_provided_extras()) == ["speed"]
    assert req.metadata.get_all("Provides-Extra") == ["speed"]
    assert req.metadata.get_all("Dynamic") == ["Provides-Extra"]
