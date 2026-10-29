from __future__ import annotations

import base64
import json
import shutil
import textwrap
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from tests.lib import (
    PipTestEnvironment,
    create_basic_wheel_for_package,
    git_commit,
    make_wheel,
)
from tests.lib.filesystem import create_file


@pytest.fixture
def source_project(script: PipTestEnvironment) -> Path:
    project = script.scratch_path / "project"
    project.mkdir()
    wheel = make_wheel(
        "package",
        "1.0",
        metadata_updates={
            "Requires-Python": ">=99",
            "Requires-Dist": "unused-dependency==99",
        },
        extra_files={"package/__init__.py": ""},
    )
    create_file(project / "wheel-data", base64.b64encode(wheel.as_bytes()).decode())
    editable_wheel = make_wheel(
        "package",
        "1.0",
        metadata_updates={
            "Requires-Python": ">=99",
            "Requires-Dist": "unused-dependency==99",
        },
        extra_files={"package.pth": str(project) + "\n"},
    )
    create_file(
        project / "editable-wheel-data",
        base64.b64encode(editable_wheel.as_bytes()).decode(),
    )
    create_file(project / "package/__init__.py", "value = 1\n")
    create_file(
        project / "pyproject.toml",
        '[build-system]\nrequires = []\nbuild-backend = "backend"\n'
        'backend-path = ["."]\n',
    )
    create_file(
        project / "backend.py",
        textwrap.dedent("""\
            import base64
            import os
            from pathlib import Path

            def record(event):
                with open(os.environ["PIP_TEST_BUILD_EVENTS"], "a") as f:
                    f.write(event + "\\n")

            def get_requires_for_build_wheel(config_settings=None):
                record("wheel-requirements")
                return []

            def get_requires_for_build_editable(config_settings=None):
                record("editable-requirements")
                return []

            def prepare_metadata_for_build_wheel(*args, **kwargs):
                raise AssertionError("Separate metadata hook called")

            def prepare_metadata_for_build_editable(*args, **kwargs):
                raise AssertionError("Separate editable metadata hook called")

            def write_wheel(
                wheel_directory, config_settings, metadata_directory, editable=False
            ):
                assert config_settings == {"mode": "fast"}
                assert metadata_directory is None
                filename = "package-1.0-py2.py3-none-any.whl"
                source = "editable-wheel-data" if editable else "wheel-data"
                data = base64.b64decode(Path(source).read_bytes())
                (Path(wheel_directory) / filename).write_bytes(data)
                return filename

            def build_wheel(
                wheel_directory, config_settings=None, metadata_directory=None
            ):
                record("wheel")
                return write_wheel(wheel_directory, config_settings, metadata_directory)

            def build_editable(
                wheel_directory, config_settings=None, metadata_directory=None
            ):
                record("editable")
                return write_wheel(
                    wheel_directory, config_settings, metadata_directory, editable=True
                )
            """),
    )
    script.environ["PIP_TEST_BUILD_EVENTS"] = str(script.scratch_path / "events")
    return project


def enable_metadata_hook(project: Path, *, event_prefix: str = "") -> None:
    contents = base64.b64decode(project.joinpath("wheel-data").read_bytes())
    with ZipFile(BytesIO(contents)) as wheel:
        project.joinpath("generated-metadata").write_bytes(
            wheel.read("package-1.0.dist-info/METADATA")
        )
    backend = project / "backend.py"
    backend.write_text(
        backend.read_text()
        .replace(
            "def prepare_metadata_for_build_wheel(*args, **kwargs):\n"
            '    raise AssertionError("Separate metadata hook called")\n',
            "def prepare_metadata_for_build_wheel(\n"
            "    metadata_directory, config_settings=None\n"
            "):\n"
            '    record("metadata")\n'
            '    target = Path(metadata_directory) / "package-1.0.dist-info"\n'
            "    target.mkdir()\n"
            '    contents = Path("generated-metadata").read_bytes()\n'
            '    (target / "METADATA").write_bytes(contents)\n'
            "    return target.name\n",
        )
        .replace(
            'f.write(event + "\\n")',
            f'f.write({event_prefix!r} + event + "\\n")',
        )
        .replace("assert metadata_directory is None", "pass")
    )


@pytest.mark.parametrize(
    "command, source_kind, editable",
    [
        ("install", "directory", False),
        ("wheel", "directory", False),
        ("install", "directory", True),
        ("install", "vcs", False),
        ("wheel", "vcs", False),
        ("install", "archive", False),
        ("wheel", "archive", False),
    ],
)
def test_build_once_for_metadata(
    script: PipTestEnvironment,
    source_project: Path,
    command: str,
    source_kind: str,
    editable: bool,
) -> None:
    if source_kind == "vcs":
        script.run("git", "init", "--quiet", cwd=source_project)
        script.run("git", "add", ".", cwd=source_project)
        git_commit(source_project)
        requirement = f"git+{source_project.as_uri()}"
        if command == "install":
            requirement = f"package @ {requirement}"
    elif source_kind == "archive":
        requirement = shutil.make_archive(
            str(script.scratch_path / "snapshot"), "gztar", root_dir=source_project
        )
    else:
        requirement = str(source_project)
    editable_args = ["--editable"] if editable else []
    result = script.pip(
        command,
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--config-settings=mode=fast",
        *editable_args,
        requirement,
    )
    events = script.scratch_path.joinpath("events").read_text().splitlines()
    assert events == (
        ["editable-requirements", "editable"]
        if editable
        else ["wheel-requirements", "wheel"]
    )
    if command == "wheel":
        result.did_create(script.scratch / "package-1.0-py2.py3-none-any.whl")
        return
    result.assert_installed("package", editable=editable, editable_vcs=not editable)
    origin = result.get_created_direct_url("package")
    assert origin is not None
    if source_kind == "vcs":
        assert origin.url == source_project.as_uri()
        assert origin.vcs_info is not None
        assert origin.vcs_info.vcs == "git"
        assert (
            origin.vcs_info.commit_id
            == script.run("git", "rev-parse", "HEAD", cwd=source_project).stdout.strip()
        )
    elif source_kind == "archive":
        assert origin.url == Path(requirement).as_uri()
        assert origin.archive_info is not None
        assert origin.archive_info.hashes is not None
        assert (
            origin.archive_info.hashes["sha256"]
            == sha256(Path(requirement).read_bytes()).hexdigest()
        )
    else:
        assert origin.url == source_project.as_uri()
        assert origin.dir_info is not None
        assert origin.is_local_editable() is editable
    if editable:
        imported = script.run("python", "-c", "import package; print(package.value)")
        assert imported.stdout.strip() == "1"
        source_project.joinpath("package/__init__.py").write_text("value = 200\n")
        imported = script.run("python", "-c", "import package; print(package.value)")
        assert imported.stdout.strip() == "200"


@pytest.mark.parametrize(
    "options",
    [
        ["--no-deps"],
        ["--ignore-requires-python"],
        ["--no-deps", "--ignore-requires-python", "--dry-run"],
        ["--no-deps", "--report", "report.json"],
        ["--ignore-requires-python", "--report", "report.json"],
        [
            "--no-deps",
            "--ignore-requires-python",
            "--dry-run",
            "--report",
            "report.json",
        ],
    ],
)
def test_metadata_hook_is_retained_when_needed(
    script: PipTestEnvironment, source_project: Path, options: list[str]
) -> None:
    result = script.pip(
        "install",
        "--no-index",
        "--config-settings=mode=fast",
        *options,
        source_project,
        expect_error=True,
    )
    assert "Separate metadata hook called" in result.stderr
    assert script.scratch_path.joinpath("events").read_text().splitlines() == [
        "wheel-requirements"
    ]


@pytest.mark.parametrize("conflict", ["name", "version"])
def test_built_identity_still_obeys_requirements(
    script: PipTestEnvironment, source_project: Path, conflict: str
) -> None:
    name = "wrong" if conflict == "name" else "package"
    constraints = []
    if conflict == "version":
        constraint = script.temporary_file("constraints.txt", "package==2.0\n")
        constraints = ["--constraint", str(constraint)]
    result = script.pip(
        "install",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--config-settings=mode=fast",
        *constraints,
        f"{name} @ {source_project.as_uri()}",
        expect_error=True,
    )
    assert script.scratch_path.joinpath("events").read_text().splitlines() == [
        "wheel-requirements",
        "wheel",
    ]
    result.did_not_create(script.site_packages / "package")


def test_optional_metadata_hook_fallback_still_builds_once(
    script: PipTestEnvironment, source_project: Path
) -> None:
    wheel = make_wheel("package", "1.0", extra_files={"package/__init__.py": ""})
    source_project.joinpath("wheel-data").write_text(
        base64.b64encode(wheel.as_bytes()).decode()
    )
    backend = source_project / "backend.py"
    backend.write_text(
        backend.read_text().replace(
            "def prepare_metadata_for_build_wheel(*args, **kwargs):\n"
            '    raise AssertionError("Separate metadata hook called")\n',
            "",
        )
    )
    result = script.pip(
        "install",
        "--no-index",
        "--no-deps",
        "--config-settings=mode=fast",
        source_project,
    )
    assert script.scratch_path.joinpath("events").read_text().splitlines() == [
        "wheel-requirements",
        "wheel",
    ]
    result.assert_installed("package", editable=False)


def test_direct_wheel_installs_build_requirements(
    script: PipTestEnvironment, source_project: Path
) -> None:
    create_basic_wheel_for_package(script, "build_dependency", "1.0")
    backend = source_project / "backend.py"
    backend.write_text(
        backend.read_text()
        .replace("return []", 'return ["build_dependency==1.0"]')
        .replace(
            '    assert config_settings == {"mode": "fast"}',
            "    import build_dependency\n"
            '    assert config_settings == {"mode": "fast"}',
        )
    )
    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--no-deps",
        "--ignore-requires-python",
        "--config-settings=mode=fast",
        source_project,
    )
    assert script.scratch_path.joinpath("events").read_text().splitlines() == [
        "wheel-requirements",
        "wheel",
    ]
    result.assert_installed("package", editable=False)


@pytest.mark.parametrize("defect", ["filename", "metadata-version"])
@pytest.mark.parametrize("verify", [False, True])
def test_direct_wheel_respects_no_verify(
    script: PipTestEnvironment, source_project: Path, defect: str, verify: bool
) -> None:
    if defect == "filename":
        filename = "package-2.0-py2.py3-none-any.whl"
        backend = source_project / "backend.py"
        backend.write_text(
            backend.read_text().replace(
                'filename = "package-1.0-py2.py3-none-any.whl"',
                f'filename = "{filename}"',
            )
        )
    else:
        filename = "package-1.0-py2.py3-none-any.whl"
        wheel = make_wheel("package", "1.0", metadata="Name: package\nVersion: 1.0\n")
        source_project.joinpath("wheel-data").write_text(
            base64.b64encode(wheel.as_bytes()).decode()
        )
    options = [] if verify else ["--no-verify"]
    result = script.pip(
        "wheel",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--config-settings=mode=fast",
        *options,
        source_project,
        expect_error=verify,
    )
    assert script.scratch_path.joinpath("events").read_text().splitlines() == [
        "wheel-requirements",
        "wheel",
    ]
    if verify:
        result.did_not_create(script.scratch / filename)
    else:
        result.did_create(script.scratch / filename)


@pytest.mark.parametrize("source_kind", ["directory", "archive", "vcs"])
@pytest.mark.parametrize("named", [False, True])
def test_installed_sources_do_not_build_for_metadata_when_skipped(
    script: PipTestEnvironment,
    source_project: Path,
    source_kind: str,
    named: bool,
) -> None:
    contents = base64.b64decode(source_project.joinpath("wheel-data").read_bytes())
    installed = script.scratch_path / "package-1.0-py2.py3-none-any.whl"
    installed.write_bytes(contents)
    script.pip(
        "install", "--no-index", "--no-deps", "--ignore-requires-python", installed
    )
    enable_metadata_hook(source_project)
    if source_kind == "vcs":
        script.run("git", "init", "--quiet", cwd=source_project)
        script.run("git", "add", ".", cwd=source_project)
        git_commit(source_project)
        requirement = f"git+{source_project.as_uri()}"
    elif source_kind == "archive":
        requirement = Path(
            shutil.make_archive(
                str(script.scratch_path / "snapshot"), "gztar", root_dir=source_project
            )
        ).as_uri()
    else:
        requirement = source_project.as_uri()
    if named:
        requirement = f"package @ {requirement}"
    script.pip(
        "install",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--config-settings=mode=fast",
        requirement,
    )
    events = script.scratch_path / "events"
    observed = events.read_text().splitlines() if events.exists() else []
    if source_kind == "vcs":
        assert observed == ["wheel-requirements", "metadata"]
    else:
        assert observed == ["wheel-requirements", "wheel"]


@pytest.mark.parametrize("guarantee", ["--force-reinstall", "--ignore-installed"])
def test_bare_vcs_with_guaranteed_build(
    script: PipTestEnvironment, source_project: Path, guarantee: str
) -> None:
    script.run("git", "init", "--quiet", cwd=source_project)
    script.run("git", "add", ".", cwd=source_project)
    git_commit(source_project)
    result = script.pip(
        "install",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        guarantee,
        "--config-settings=mode=fast",
        f"git+{source_project.as_uri()}",
    )
    assert script.scratch_path.joinpath("events").read_text().splitlines() == [
        "wheel-requirements",
        "wheel",
    ]
    result.assert_installed("package", editable=False)


def test_missing_built_metadata_artifact_has_no_traceback(
    script: PipTestEnvironment, source_project: Path
) -> None:
    backend = source_project / "backend.py"
    backend.write_text(
        backend.read_text().replace(
            "(Path(wheel_directory) / filename).write_bytes(data)", "pass"
        )
    )
    result = script.pip(
        "install",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--config-settings=mode=fast",
        source_project,
        expect_error=True,
    )
    assert "Traceback" not in result.stderr
    assert "invalid" in result.stderr


@pytest.mark.parametrize("command", ["install", "wheel"])
def test_legacy_resolver_keeps_normal_metadata_preparation(
    script: PipTestEnvironment, source_project: Path, command: str
) -> None:
    enable_metadata_hook(source_project)
    result = script.pip(
        command,
        "--use-deprecated=legacy-resolver",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--config-settings=mode=fast",
        f"package @ {source_project.as_uri()}",
    )
    assert script.scratch_path.joinpath("events").read_text().splitlines() == [
        "wheel-requirements",
        "metadata",
        "wheel",
    ]
    if command == "install":
        result.assert_installed("package", editable=False)
    else:
        result.did_create(script.scratch / "package-1.0-py2.py3-none-any.whl")


@pytest.mark.parametrize("source_kind", ["directory", "editable", "archive"])
def test_actual_install_report_uses_complete_built_wheel_metadata(
    script: PipTestEnvironment, source_project: Path, source_kind: str
) -> None:
    metadata = (
        "Metadata-Version: 2.2\nName: package\nVersion: 1.0\n"
        "Requires-Python: >=99\nRequires-Dist: unused-dependency==99\n"
        "Summary: café\nProject-URL: Docs, https://example.invalid/docs\n"
        "\nComplete report description.\n"
    )
    wheel = make_wheel(
        "package",
        "1.0",
        metadata=metadata,
        extra_files=(
            {"package.pth": str(source_project) + "\n"}
            if source_kind == "editable"
            else {"package/__init__.py": ""}
        ),
    )
    source_project.joinpath(
        "editable-wheel-data" if source_kind == "editable" else "wheel-data"
    ).write_bytes(base64.b64encode(wheel.as_bytes()))
    if source_kind == "archive":
        requirement = Path(
            shutil.make_archive(
                str(script.scratch_path / "snapshot"), "gztar", root_dir=source_project
            )
        ).as_uri()
    else:
        requirement = source_project.as_uri()
    editable_args = ["--editable"] if source_kind == "editable" else []
    result = script.pip(
        "install",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--config-settings=mode=fast",
        "--report",
        "report.json",
        *editable_args,
        requirement,
    )
    expected = (
        ["editable-requirements", "editable"]
        if source_kind == "editable"
        else ["wheel-requirements", "wheel"]
    )
    assert script.scratch_path.joinpath("events").read_text().splitlines() == expected
    report = json.loads(script.scratch_path.joinpath("report.json").read_text())
    assert len(report["install"]) == 1
    item = report["install"][0]
    assert item["metadata"]["name"] == "package"
    assert item["metadata"]["version"] == "1.0"
    assert item["metadata"]["requires_python"] == ">=99"
    assert item["metadata"]["requires_dist"] == ["unused-dependency==99"]
    assert item["metadata"]["summary"] == "café"
    assert item["metadata"]["project_url"] == ["Docs, https://example.invalid/docs"]
    assert item["metadata"]["description"] == "Complete report description.\n"
    assert item["download_info"]["url"] == requirement
    assert item["requested"] is True
    if source_kind == "archive":
        archive = script.scratch_path / "snapshot.tar.gz"
        assert item["download_info"]["archive_info"]["hashes"] == {
            "sha256": sha256(archive.read_bytes()).hexdigest()
        }
    else:
        assert item["download_info"]["dir_info"] == (
            {"editable": True} if source_kind == "editable" else {}
        )
    result.assert_installed(
        "package", editable=(source_kind == "editable"), editable_vcs=False
    )


def test_actual_report_does_not_eagerly_build_an_installed_vcs_source(
    script: PipTestEnvironment, source_project: Path
) -> None:
    installed = script.scratch_path / "package-1.0-py2.py3-none-any.whl"
    installed.write_bytes(
        base64.b64decode(source_project.joinpath("wheel-data").read_bytes())
    )
    script.pip(
        "install", "--no-index", "--no-deps", "--ignore-requires-python", installed
    )
    enable_metadata_hook(source_project)
    script.run("git", "init", "--quiet", cwd=source_project)
    script.run("git", "add", ".", cwd=source_project)
    git_commit(source_project)
    script.pip(
        "install",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--config-settings=mode=fast",
        "--report",
        "report.json",
        f"package @ git+{source_project.as_uri()}",
    )
    report = json.loads(script.scratch_path.joinpath("report.json").read_text())
    assert report["install"] == []
    events = script.scratch_path / "events"
    observed = events.read_text().splitlines() if events.exists() else []
    assert observed == ["wheel-requirements", "metadata"]
