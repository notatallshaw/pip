import hashlib
from pathlib import Path

import pytest

from pip._vendor.packaging import markers

from pip._internal.exceptions import HashMismatch, InstallationError
from pip._internal.models.target_python import TargetPython
from pip._internal.req.constructors import install_req_from_line
from pip._internal.req.dependency_sources import (
    DependencySources,
    choose_sources,
    read_sources,
)
from pip._internal.utils.hashes import Hashes


@pytest.mark.parametrize(
    "body",
    [
        'core = {path = "core.whl", editable = true}',
        'core = {url = "https://example.org/x.zip", hashes = {sha256 = "abcd"}}',
        'core = {url = "https://example.org/x.zip", hashes = {unknown = "abcd"}}',
    ],
)
def test_invalid_sources(tmp_path: Path, body: str) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text("[develop.sources]\n" + body)
    with pytest.raises(InstallationError):
        read_sources(project)


def test_scope_and_declaring_file_paths(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    target = root / "app"
    target.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[develop.sources]\nancestor = "wrong"')
    (root / "pyproject.toml").write_text(
        '[develop.sources]\nCore = "root-core"\nother = "root-other"'
    )
    project = target / "pyproject.toml"
    project.write_text('[develop.sources]\ncore = "../local-core"')

    sources = DependencySources(project, root)
    core = sources.select("CORE")
    other = sources.select("other")
    assert core is not None
    assert other is not None
    assert core.location == str(root / "local-core")
    assert other.location == str(root / "root-other")
    assert sources.select("ancestor") is None

    from_target = DependencySources(project, target)
    target_core = from_target.select("core")
    assert target_core is not None
    assert target_core.location == str(root / "local-core")
    assert from_target.select("other") is None


def test_unknown_develop_key_and_directory_hashes(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text("[develop]\nunknown = true")
    with pytest.raises(InstallationError):
        read_sources(project)
    project.write_text(
        '[develop.sources]\ncore = {path = ".", hashes = {sha256 = "' + "0" * 64 + '"}}'
    )
    with pytest.raises(InstallationError):
        read_sources(project)


@pytest.mark.parametrize("kind", ["git", "hg", "bzr", "svn"])
def test_vcs_links(tmp_path: Path, kind: str) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        f'[develop.sources]\ncore = {{{kind} = "https://example.org/core", '
        'rev = "stable", subdirectory = "packages/core"}'
    )
    source = read_sources(project)["core"][0]
    assert (
        source.as_link().url
        == f"{kind}+https://example.org/core@stable#subdirectory=packages/core"
    )


@pytest.mark.parametrize("ref,expected", [("branch", "heads"), ("tag", "tags")])
def test_git_ref_namespace(tmp_path: Path, ref: str, expected: str) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[develop.sources]\ncore = {git = "https://example.org/core", '
        f'{ref} = "stable"}}'
    )
    assert (
        read_sources(project)["core"][0]
        .as_link()
        .url.endswith(f"@refs/{expected}/stable")
    )


def test_credential_expansion_only_in_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOURCE_TOKEN", "secret@with/slash")
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[develop.sources]\ncore = {index = "https://${SOURCE_TOKEN}@example.org/${SOURCE_TOKEN}/"}'
    )
    source = DependencySources(project, tmp_path).select("core")
    assert source is not None
    assert (
        source.location == "https://secret%40with%2Fslash@example.org/${SOURCE_TOKEN}/"
    )
    assert "secret" not in str(source)


def test_ordered_selectors_and_input_copy(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[develop.sources]\ncore = [{path = "special", group = "test"}, '
        '{path = "default"}]'
    )
    sources = DependencySources(project, tmp_path)
    special = sources.select("core", group="test")
    assert special is not None
    assert special.location.endswith("special")
    source = sources.select("core")
    assert source is not None
    assert source.location.endswith("default")
    original = install_req_from_line('core[fast]>=1; python_version >= "3"')
    redirected = sources.apply(original, source)
    assert redirected.link is not None
    assert redirected.req is not None
    assert original.req is not None
    assert original.link is None
    assert redirected.link.file_path == source.location
    assert redirected.extras == {"fast"}
    assert redirected.markers == original.markers
    assert not redirected.req.specifier
    assert str(original.req.specifier) == ">=1"


def test_every_supported_hash_must_match() -> None:
    data = b"source archive"
    sha256 = hashlib.sha256(data).hexdigest()
    sha512 = hashlib.sha512(data).hexdigest()
    hashes = Hashes(required=frozenset({("sha256", sha256), ("sha512", sha512)}))
    hashes.check_against_chunks([data[:3], data[3:]])
    assert not hashes.has_one_of({"sha256": sha256})
    assert hashes.has_one_of({"sha256": sha256, "sha512": sha512})

    bad = Hashes(required=frozenset({("sha256", sha256), ("sha512", "0" * 128)}))
    with pytest.raises(HashMismatch):
        bad.check_against_chunks([data])
    with pytest.raises(HashMismatch):
        (hashes & bad).check_against_chunks([data])


def test_source_hashes_do_not_activate_global_hash_mode(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[develop.sources]\ncore = {url = "https://example.org/core.zip", '
        'hashes = {sha256 = "' + "0" * 64 + '"}}'
    )
    sources = DependencySources(project, tmp_path)
    source = sources.select("core")
    assert source is not None
    redirected = sources.apply(install_req_from_line("core"), source)
    assert not redirected.has_hash_options
    assert redirected.hashes()


def test_inactive_sources_do_not_require_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UNUSED_SOURCE_TOKEN", raising=False)
    project = tmp_path / "pyproject.toml"
    project.write_text(
        "[develop.sources]\ncore = {index = "
        '"https://${UNUSED_SOURCE_TOKEN}@example.org/simple/", '
        "marker = \"python_version < '1'\"}"
    )
    sources = DependencySources(project, tmp_path)
    assert sources.select("core") is None


@pytest.mark.parametrize("explicit", [False, True])
def test_parent_components_do_not_make_an_outside_target_a_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool
) -> None:
    app = tmp_path / "app"
    tools = tmp_path / "tools"
    app.mkdir()
    tools.mkdir()
    (app / "pyproject.toml").write_text('[dependency-groups]\ntest = ["core"]')
    (tools / "pyproject.toml").write_text('[develop.sources]\ncore = "wrong"')
    monkeypatch.chdir(tools)
    path = "../app/pyproject.toml"
    sources = choose_sources([], [(path, "test")], path if explicit else None)
    assert sources is not None
    assert sources.select("core") is None


def test_manifest_symlink_keeps_its_declaring_directory(tmp_path: Path) -> None:
    original = tmp_path / "original"
    project = tmp_path / "project"
    original.mkdir()
    project.mkdir()
    manifest = original / "pyproject.toml"
    manifest.write_text('[develop.sources]\ncore = "local"')
    link = project / "pyproject.toml"
    try:
        link.symlink_to(manifest)
    except OSError:
        pytest.skip("Creating symlinks is unavailable")
    source = DependencySources(link, project).select("core")
    assert source is not None
    assert source.location == str(project / "local")


def test_parent_components_follow_directory_symlinks(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    (outside / "inner").mkdir(parents=True)
    for directory in (outside / "app", project / "app"):
        directory.mkdir()
        (directory / "pyproject.toml").write_text('[develop.sources]\ncore = "local"')
    alias = project / "alias"
    try:
        alias.symlink_to(outside / "inner", target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is unavailable")
    manifest = alias / ".." / "app" / "pyproject.toml"
    source = DependencySources(manifest, project).select("core")
    assert source is not None
    # Windows normalizes parent components before following directory symlinks.
    selected = (
        outside if manifest.samefile(outside / "app" / "pyproject.toml") else project
    )
    assert source.location == str(selected / "app" / "local")


def test_default_target_keeps_full_host_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = markers.default_environment()
    environment["python_full_version"] = "3.15.0a1"
    monkeypatch.setattr(markers, "default_environment", lambda: environment.copy())
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[develop.sources]\ncore = {path = "local", '
        "marker = \"python_full_version == '3.15.0a1'\"}"
    )
    sources = DependencySources(project, tmp_path)
    sources.set_target_environment(TargetPython())
    assert sources.select("core") is not None


def test_explicit_cpython_version_sets_implementation_marker(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[develop.sources]\ncore = {path = "local", '
        "marker = \"implementation_version == '3.10.0'\"}"
    )
    sources = DependencySources(project, tmp_path)
    sources.set_target_environment(
        TargetPython(py_version_info=(3, 10), implementation="cp")
    )
    assert sources.select("core") is not None


def test_unknown_target_implementation_version_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[develop.sources]\ncore = {path = "local", '
        "marker = \"implementation_version == '7.3'\"}"
    )
    sources = DependencySources(project, tmp_path)
    with pytest.raises(InstallationError, match="implementation_version"):
        sources.set_target_environment(
            TargetPython(py_version_info=(3, 10), implementation="pp")
        )
