import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from tests.lib import PipTestEnvironment, _create_test_package, make_wheel


def local_project(directory: Path, name: str, body: str = "") -> Path:
    """Create a setuptools project for metadata, editable and build controls."""
    directory.mkdir()
    (directory / "setup.py").write_text("from setuptools import setup\nsetup()\n")
    (directory / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools"]\n'
        'build-backend = "setuptools.build_meta"\n'
        f'[project]\nname = "{name}"\nversion = "1.0"\n{body}'
    )
    return directory


def source_project(
    script: PipTestEnvironment, source: str, dependencies: str = '"core"'
) -> Path:
    """Create a source-only project with a dependency group for CLI controls."""
    project = script.scratch_path / "pyproject.toml"
    project.write_text(
        f"[dependency-groups]\ntest = [{dependencies}]\n"
        f"[develop.sources]\ncore = {source}\n"
    )
    return project


@pytest.mark.parametrize("command", ["install", "download", "wheel", "lock"])
def test_source_wheel_all_commands(script: PipTestEnvironment, command: str) -> None:
    wheel = Path(make_wheel("core", "1.0").save_to_dir(script.scratch_path))
    source_project(script, json.dumps(wheel.name))
    if command == "install":
        result = script.pip(
            "install", "--no-index", "--group", "test", cwd=script.scratch_path
        )
        info = result.get_created_direct_url("core")
        assert info is not None
        assert info.url == wheel.as_uri()
    else:
        script.pip(
            command,
            "--no-index",
            "--group",
            "test",
            cwd=script.scratch_path,
            allow_stderr_warning=command == "lock",
        )


def test_no_sources_ignores_even_invalid_tables(script: PipTestEnvironment) -> None:
    source_project(script, "{unknown = true}")
    failed = script.pip(
        "install",
        "--no-index",
        "--group",
        "test",
        cwd=script.scratch_path,
        expect_error=True,
    )
    assert failed.returncode == 1
    assert "Traceback" not in failed.stderr

    make_wheel("core", "2.0").save_to_dir(script.scratch_path)
    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--no-sources",
        "--group",
        "test",
        cwd=script.scratch_path,
    )
    assert not result.get_created_direct_url("core")


def test_transitive_sources_and_preserved_metadata(script: PipTestEnvironment) -> None:
    core = Path(make_wheel("core", "1.0").save_to_dir(script.scratch_path))
    outer = Path(
        make_wheel(
            "outer", "1.0", metadata_updates={"Requires-Dist": ["core>=1"]}
        ).save_to_dir(script.scratch_path)
    )
    project = source_project(script, json.dumps(core.name))
    result = script.pip(
        "install", "--no-index", "--sources", project, outer, cwd=script.scratch_path
    )
    info = result.get_created_direct_url("core")
    assert info is not None
    assert info.url == core.as_uri()
    metadata = script.site_packages_path / "outer-1.0.dist-info" / "METADATA"
    assert "Requires-Dist: core>=1" in metadata.read_text()


def test_built_metadata_is_identical_with_sources_disabled(
    script: PipTestEnvironment,
) -> None:
    root = script.scratch_path
    private = root / "private"
    public = root / "public"
    private.mkdir()
    public.mkdir()
    make_wheel("core", "1.0").save_to_dir(private)
    make_wheel("core", "2.0").save_to_dir(public)
    source_wheel = next(private.glob("*.whl"))
    app = local_project(
        root / "app",
        "app",
        'dependencies = ["core>=1"]\n'
        f'[develop.sources]\ncore = {{path = "../private/{source_wheel.name}"}}\n',
    )
    metadata = []
    for name, options, expected_version in (
        ("develop", [], "1.0"),
        ("published", ["--no-sources"], "2.0"),
    ):
        output = root / name
        script.pip(
            "wheel",
            "--no-index",
            "--find-links",
            public,
            "--no-build-isolation",
            "--wheel-dir",
            output,
            *options,
            app,
            cwd=root,
        )
        assert list(output.glob(f"core-{expected_version}-*.whl"))
        with ZipFile(next(output.glob("app-*.whl"))) as wheel:
            metadata.append(wheel.read("app-1.0.dist-info/METADATA"))
    assert metadata[0] == metadata[1]
    assert b"Requires-Dist: core>=1" in metadata[0]


def test_source_version_must_satisfy_original_requirement(
    script: PipTestEnvironment,
) -> None:
    wheel = Path(make_wheel("core", "1.0").save_to_dir(script.scratch_path))
    source_project(script, json.dumps(wheel.name), '"core>=2"')
    result = script.pip(
        "install",
        "--no-index",
        "--group",
        "test",
        cwd=script.scratch_path,
        expect_error=True,
    )
    assert result.returncode != 0

    assert "required version >=2" in result.stderr


def test_original_markers_and_requested_extras(script: PipTestEnvironment) -> None:
    core = Path(
        make_wheel(
            "core",
            "1.0",
            metadata_updates={
                "Provides-Extra": ["fast"],
                "Requires-Dist": ['speed; extra == "fast"'],
            },
        ).save_to_dir(script.scratch_path)
    )
    make_wheel("speed", "1.0").save_to_dir(script.scratch_path)
    source_project(
        script, json.dumps(core.name), "'core[fast]', 'skipped; python_version < \"1\"'"
    )
    script.pip(
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--group",
        "test",
        cwd=script.scratch_path,
    )
    script.assert_installed(core="1.0", speed="1.0")


def test_source_hashes_all_checked_without_global_mode(
    script: PipTestEnvironment,
) -> None:
    core = Path(
        make_wheel(
            "core", "1.0", metadata_updates={"Requires-Dist": ["other"]}
        ).save_to_dir(script.scratch_path)
    )
    make_wheel("other", "1.0").save_to_dir(script.scratch_path)
    sha256 = hashlib.sha256(core.read_bytes()).hexdigest()
    source_project(
        script,
        f'{{path = "{core.name}", hashes = {{sha256 = "{sha256}", sha512 = "'
        + "0" * 128
        + '"}}',
    )
    args = (
        "install",
        "--no-index",
        "--find-links",
        script.scratch_path,
        "--group",
        "test",
    )
    bad = script.pip(*args, cwd=script.scratch_path, expect_error=True)
    assert "sha512" in bad.stderr

    sha512 = hashlib.sha512(core.read_bytes()).hexdigest()
    project = script.scratch_path / "pyproject.toml"
    project.write_text(project.read_text().replace("0" * 128, sha512))
    script.pip(*args, cwd=script.scratch_path)
    script.assert_installed(core="1.0", other="1.0")


def test_group_source_conflicts(script: PipTestEnvironment) -> None:
    one = Path(make_wheel("core", "1.0").save_to_dir(script.scratch_path))
    two = Path(make_wheel("core", "2.0").save_to_dir(script.scratch_path))
    project = script.scratch_path / "pyproject.toml"
    project.write_text(
        '[dependency-groups]\na = ["core"]\nb = ["core"]\n'
        f'[develop.sources]\ncore = [{{path = "{one.name}", group = "a"}}, '
        f'{{path = "{two.name}", group = "b"}}]'
    )
    result = script.pip(
        "install",
        "--no-index",
        "--group",
        "a",
        "--group",
        "b",
        cwd=script.scratch_path,
        expect_error=True,
    )
    assert result.returncode != 0


def test_conflicting_indexes_are_not_merged(script: PipTestEnvironment) -> None:
    root = script.scratch_path
    urls = []
    for name in ("one", "two"):
        package = root / name / "core"
        package.mkdir(parents=True)
        wheel = Path(make_wheel("core", "1.0").save_to_dir(package))
        (package / "index.html").write_text(f'<a href="{wheel.name}">{wheel.name}</a>')
        urls.append(package.parent.as_uri())
    project = root / "pyproject.toml"
    project.write_text(
        '[dependency-groups]\na = ["core"]\nb = ["core"]\n'
        f'[develop.sources]\ncore = [{{index = "{urls[0]}", group = "a"}}, '
        f'{{index = "{urls[1]}", group = "b"}}]\n'
    )
    failed = script.pip(
        "install",
        "--index-url",
        urls[0],
        "--group",
        "a",
        "--group",
        "b",
        cwd=root,
        expect_error=True,
    )
    assert failed.returncode != 0


def test_index_sources_do_not_fall_back_or_leak(script: PipTestEnvironment) -> None:
    root = script.scratch_path
    index = root / "index"
    package = index / "core"
    package.mkdir(parents=True)
    core = Path(
        make_wheel(
            "core", "1.0", metadata_updates={"Requires-Dist": ["other"]}
        ).save_to_dir(package)
    )
    (package / "index.html").write_text(f'<a href="{core.name}">{core.name}</a>')
    other_dir = index / "other"
    other_dir.mkdir()
    other = Path(make_wheel("other", "99.0").save_to_dir(other_dir))
    (other_dir / "index.html").write_text(f'<a href="{other.name}">{other.name}</a>')
    project = source_project(script, '{index = "' + index.as_uri() + '"}')
    make_wheel("core", "9.0").save_to_dir(root)
    make_wheel("other", "2.0").save_to_dir(root)
    default_index = root / "default-index"
    default_index.mkdir()
    default_other = default_index / "other"
    default_other.mkdir()
    (default_other / "index.html").write_text("")
    report = root / "report.json"
    script.pip(
        "install",
        "--index-url",
        default_index.as_uri(),
        "--sources",
        project,
        "--find-links",
        root,
        "--dry-run",
        "--ignore-installed",
        "--report",
        report,
        "core<2",
        cwd=root,
    )
    installed = {
        entry["metadata"]["name"]: entry["metadata"]["version"]
        for entry in json.loads(report.read_text())["install"]
    }
    assert installed == {"core": "1.0", "other": "2.0"}

    # An empty pinned index must not admit a newer fallback wheel.
    (package / "index.html").write_text("")
    result = script.pip(
        "install",
        "--index-url",
        default_index.as_uri(),
        "--sources",
        project,
        "--find-links",
        root,
        "--dry-run",
        "core",
        cwd=root,
        allow_stderr_warning=True,
        expect_error=True,
    )
    assert result.returncode != 0
    assert "from versions: 9.0" not in result.stderr


@pytest.mark.parametrize("editable", [False, True])
def test_local_project_automatically_selects_sources(
    script: PipTestEnvironment, editable: bool
) -> None:
    root = script.scratch_path
    core = local_project(root / "core", "core")
    app = local_project(
        root / "app",
        "app",
        'dependencies = ["core>=1"]\n'
        '[develop.sources]\ncore = {path = "../core", '
        f"editable = {str(editable).lower()}}}\n",
    )
    result = script.pip("install", "--no-index", "--no-build-isolation", app, cwd=root)
    info = result.get_created_direct_url("core")
    assert info is not None
    assert info.dir_info is not None
    assert info.url == core.as_uri()
    assert bool(info.dir_info.editable) == editable
    script.assert_installed(app="1.0", core="1.0")


def test_target_direct_reference_wins(script: PipTestEnvironment) -> None:
    root = script.scratch_path
    explicit = Path(make_wheel("core", "1.0").save_to_dir(root))
    other = Path(make_wheel("core", "2.0").save_to_dir(root))
    app = local_project(
        root / "app",
        "app",
        f'dependencies = ["core @ {explicit.as_uri()}"]\n'
        f'[develop.sources]\ncore = {{path = "../{other.name}"}}\n',
    )
    result = script.pip(
        "install",
        "--no-index",
        "--no-build-isolation",
        app,
        cwd=root,
        allow_stderr_warning=True,
    )
    info = result.get_created_direct_url("core")
    assert info is not None
    assert info.url == explicit.as_uri()
    script.assert_installed(core="1.0")


def test_dependency_direct_reference_is_overridden(script: PipTestEnvironment) -> None:
    root = script.scratch_path
    old = Path(make_wheel("core", "1.0").save_to_dir(root))
    new = Path(make_wheel("core", "2.0").save_to_dir(root))
    outer = Path(
        make_wheel(
            "outer",
            "1.0",
            metadata=(
                "Metadata-Version: 2.1\nName: outer\nVersion: 1.0\n"
                f"Requires-Dist: core @ {old.as_uri()}\n\n"
            ),
        ).save_to_dir(root)
    )
    project = source_project(script, json.dumps(new.name))
    result = script.pip("install", "--no-index", "--sources", project, outer, cwd=root)
    info = result.get_created_direct_url("core")
    assert info is not None
    assert info.url == new.as_uri()


def test_replacement_does_not_require_old_file_uri_support(
    script: PipTestEnvironment,
) -> None:
    root = script.scratch_path
    replacement = Path(make_wheel("core", "2.0").save_to_dir(root))
    outer = Path(
        make_wheel(
            "outer",
            "1.0",
            metadata_updates={
                "Requires-Dist": ["core @ file://old-host/core-1.0-py3-none-any.whl"]
            },
        ).save_to_dir(root)
    )
    project = source_project(script, json.dumps(replacement.name))
    result = script.pip("install", "--no-index", "--sources", project, outer, cwd=root)
    info = result.get_created_direct_url("core")
    assert info is not None
    assert info.url == replacement.as_uri()


def test_extra_selectors_are_target_local(script: PipTestEnvironment) -> None:
    root = script.scratch_path
    normal = Path(make_wheel("core", "1.0").save_to_dir(root))
    special = Path(make_wheel("core", "2.0").save_to_dir(root))
    app = local_project(
        root / "app",
        "app",
        '[project.optional-dependencies]\nfast = ["core"]\n'
        f'[develop.sources]\ncore = [{{path = "../{special.name}", extra = "fast"}}, '
        f'{{path = "../{normal.name}"}}]\n',
    )
    result = script.pip(
        "install", "--no-index", "--no-build-isolation", f"{app}[fast]", cwd=root
    )
    info = result.get_created_direct_url("core")
    assert info is not None
    assert info.url == special.as_uri()


def test_rejected_parent_does_not_leave_source_conflict(
    script: PipTestEnvironment,
) -> None:
    root = script.scratch_path
    one = Path(make_wheel("core", "1.0").save_to_dir(root))
    two = Path(make_wheel("core", "2.0").save_to_dir(root))
    make_wheel(
        "outer", "2.0", metadata_updates={"Requires-Dist": ["core"]}
    ).save_to_dir(root)
    make_wheel(
        "outer", "1.0", metadata_updates={"Requires-Dist": ["app[fast]"]}
    ).save_to_dir(root)
    app = local_project(
        root / "app",
        "app",
        '[project.optional-dependencies]\nfast = ["core"]\n'
        '[dependency-groups]\ntest = ["core", "outer"]\n'
        '[develop.sources]\napp = "."\n'
        f'core = [{{path = "../{one.name}", group = "test"}}, '
        f'{{path = "../{one.name}", extra = "fast"}}, '
        f'{{path = "../{two.name}"}}]\n',
    )
    script.pip(
        "install",
        "--no-index",
        "--find-links",
        root,
        "--no-build-isolation",
        "--group",
        str(app / "pyproject.toml") + ":test",
        cwd=root,
    )
    script.assert_installed(core="1.0", outer="1.0", app="1.0")


def test_environment_opt_out_overrides_source_project(
    script: PipTestEnvironment,
) -> None:
    root = script.scratch_path
    project = source_project(script, '{unknown = "invalid"}')
    make_wheel("core", "1.0").save_to_dir(root)
    script.environ["PIP_SOURCES"] = str(project)
    script.environ["PIP_NO_SOURCES"] = "true"
    result = script.pip("install", "--no-index", "--find-links", root, "core", cwd=root)
    assert not result.get_created_direct_url("core")


@pytest.mark.parametrize("inprocess", [False, True])
def test_build_requirements_ignore_sources(
    script: PipTestEnvironment, common_wheels: Path, inprocess: bool
) -> None:
    root = script.scratch_path
    app = local_project(root / "app", "app")
    project = app / "pyproject.toml"
    project.write_text(
        project.read_text() + '[develop.sources]\nsetuptools = "../missing-backend"\n'
    )
    script.environ["PIP_SOURCES"] = str(project)
    args = [
        "install",
        "--no-index",
        "--find-links",
        str(common_wheels),
        str(app),
    ]
    if inprocess:
        args += ["--use-feature", "inprocess-build-deps"]
    script.pip(*args, cwd=root, allow_stderr_warning=True)
    script.assert_installed(app="1.0")


def test_dependency_source_tables_are_ignored(script: PipTestEnvironment) -> None:
    root = script.scratch_path
    local_project(
        root / "core",
        "core",
        'dependencies = ["leaf"]\n[develop.sources]\nleaf = "../missing"\n',
    )
    make_wheel("leaf", "1.0").save_to_dir(root)
    source_project(script, '{path = "core"}')
    script.pip(
        "install",
        "--no-index",
        "--no-build-isolation",
        "--find-links",
        root,
        "--group",
        "test",
        cwd=root,
    )
    script.assert_installed(core="1.0", leaf="1.0")


def test_invocation_table_is_not_an_ancestor_search(script: PipTestEnvironment) -> None:
    root = script.scratch_path
    core = Path(make_wheel("core", "1.0").save_to_dir(root))
    source_project(script, json.dumps(core.name))
    app = local_project(root / "app", "app", 'dependencies = ["core"]\n')
    script.pip(
        "install", "--dry-run", "--no-index", "--no-build-isolation", app, cwd=root
    )
    failed = script.pip(
        "install",
        "--dry-run",
        "--no-index",
        "--no-build-isolation",
        ".",
        cwd=app,
        expect_error=True,
    )
    assert "No matching distribution found for core" in failed.stderr


@pytest.mark.parametrize("explicit", [False, True])
def test_sibling_target_ignores_invocation_sources(
    script: PipTestEnvironment, explicit: bool
) -> None:
    root = script.scratch_path
    app = root / "app"
    wheels = app / "wheels"
    wheels.mkdir(parents=True)
    core = Path(make_wheel("core", "1.0").save_to_dir(wheels))
    make_wheel("other", "1.0").save_to_dir(wheels)
    (app / "pyproject.toml").write_text(
        '[dependency-groups]\ntest = ["core", "other"]\n'
        f'[develop.sources]\ncore = {{path = "wheels/{core.name}", group = "test"}}\n'
    )
    tools = root / "tools"
    tools.mkdir()
    (tools / "pyproject.toml").write_text(
        '[develop.sources]\nother = "missing/other-9.0-py3-none-any.whl"'
    )
    options = ["--sources", "../app/pyproject.toml"] if explicit else []
    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        wheels,
        "--group",
        "../app/pyproject.toml:test",
        *options,
        cwd=tools,
    )
    info = result.get_created_direct_url("core")
    assert info is not None
    assert info.url == core.as_uri()
    assert not result.get_created_direct_url("other")


def test_index_pin_rejects_external_url_constraint(script: PipTestEnvironment) -> None:
    root = script.scratch_path
    index = root / "index" / "core"
    index.mkdir(parents=True)
    wheel = Path(make_wheel("core", "1.0").save_to_dir(index))
    (index / "index.html").write_text(f'<a href="{wheel.name}">{wheel.name}</a>')
    external = Path(make_wheel("core", "2.0").save_to_dir(root))
    constraints = root / "constraints.txt"
    constraints.write_text(f"core @ {external.as_uri()}")
    source_project(script, '{index = "' + index.parent.as_uri() + '"}')
    failed = script.pip(
        "install",
        "--index-url",
        index.parent.as_uri(),
        "--constraint",
        constraints,
        "--group",
        "test",
        cwd=root,
        expect_error=True,
    )
    assert failed.returncode != 0
    assert "Successfully installed" not in failed.stdout


@pytest.mark.parametrize("selector", ["tag", "branch"])
def test_git_selector_uses_correct_ref_namespace(
    script: PipTestEnvironment, selector: str
) -> None:
    root = script.scratch_path
    repository = _create_test_package(root, name="core")
    command = "tag" if selector == "tag" else "branch"
    script.run("git", command, "chosen", cwd=repository)
    source_project(
        script, '{git = "' + repository.as_uri() + '", ' + selector + ' = "chosen"}'
    )
    script.pip(
        "install", "--no-index", "--no-build-isolation", "--group", "test", cwd=root
    )
    script.assert_installed(core="0.1")

    other = "branch" if selector == "tag" else "tag"
    script.run("git", other, "wrong-kind", cwd=repository)
    project = root / "pyproject.toml"
    project.write_text(project.read_text().replace("chosen", "wrong-kind"))
    failed = script.pip(
        "install",
        "--force-reinstall",
        "--no-index",
        "--no-build-isolation",
        "--group",
        "test",
        cwd=root,
        expect_error=True,
    )
    assert failed.returncode != 0


def test_source_markers_use_explicit_target(script: PipTestEnvironment) -> None:
    root = script.scratch_path
    wheel = Path(make_wheel("core", "1.0").save_to_dir(root))
    source_project(
        script,
        '[{path = "missing", marker = "sys_platform == \'linux\'"}, {path = "'
        + wheel.name
        + "\", marker = \"python_version == '3.10' "
        "and implementation_version == '3.10.0'\"}]",
    )
    script.pip(
        "download",
        "--no-index",
        "--no-deps",
        "--platform",
        "win_amd64",
        "--implementation",
        "cp",
        "--python-version",
        "3.10",
        "--group",
        "test",
        cwd=root,
    )
