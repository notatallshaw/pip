# Development dependency sources

`[develop.sources]` selects where pip obtains dependencies while working on a project. The dependencies still belong in `[project.dependencies]`, `[project.optional-dependencies]` or `[dependency-groups]`. Source entries do not add dependencies or change the metadata written into built wheels.

For example, an application can use a sibling checkout of `core`:

```toml
[project]
name = "app"
version = "1.0"
dependencies = ["core>=1"]

[develop.sources]
core = {path = "../core", editable = true}
```

```sh
python -m pip install .
python -m pip install --no-sources .
```

The first command uses the sibling directory. The second resolves `core>=1` using the usual package locations. The installed application still declares `core>=1` in its metadata.

## Choose the project

The same source handling is available for `install`, `download`, `wheel` and `lock`:

```sh
python -m pip install ./app
python -m pip install --group app/pyproject.toml:test
python -m pip download --sources app/pyproject.toml core
```

A local project argument or dependency-group path selects that file's complete source table. `--group test` uses `pyproject.toml` in the current directory. Installing a named package, wheel or archive does not read sources from the current directory.

`--sources PATH` selects exactly one project directory or TOML file, replacing automatic selection without adding dependencies. Pip never merges source tables. For example, use a repository-wide table when installing a subproject:

```sh
python -m pip install --sources pyproject.toml ./app
```

When project arguments or dependency groups identify different files and any has sources, choose one with `--sources PATH`, or ignore all tables with `--no-sources`. Relative paths are resolved against the selected file's directory. Pip does not search other directories or read source tables in dependencies or downloaded source distributions.

## Source kinds

```toml
[develop.sources]
core = "../core"
plugin = {path = "../plugins", subdirectory = "plugin", editable = true}
widget = {path = "vendor/widget-1.0-py3-none-any.whl"}
client = {git = "https://example.org/client.git", tag = "v1.0"}
legacy = {hg = "https://example.org/legacy", rev = "stable"}
internal = {index = "https://packages.example.org/simple/"}
```

Paths can identify directories, wheels or source archives. Editable sources must be directories. Git accepts one of `rev`, `tag` or `branch`; `hg`, `bzr` and `svn` accept `rev`. Omit the VCS prefix such as `git+` from the repository URL. `subdirectory` selects a project within a directory, repository or archive.

An index source restricts that package to the specified index. Pip does not fall back to other indexes or `--find-links` for that package. Other packages keep their usual locations. `--no-index` also disables source indexes.

An archive or wheel can declare expected hashes:

```toml
[develop.sources]
core = {url = "https://example.org/core.zip", hashes = {sha256 = "<hex digest>"}}
```

Replace the placeholder with the file's digest. Pip checks every supported algorithm listed, and rejects mismatches or a table with no supported algorithm. These hashes do not switch unrelated dependencies into `--require-hashes` mode. Explicit `--require-hashes` continues to apply.

## Select different sources

```toml
[develop.sources]
core = [
  {path = "../core-debug", group = "test"},
  {index = "https://packages.example.org/simple/", marker = "sys_platform == 'linux'"},
  {path = "../core"},
]
```

The first matching entry wins. `marker`, `extra` and `group` restrict an entry; all its selectors must match. `extra` and `group` refer to occurrences in the target's own optional dependencies or dependency groups. An entry cannot combine those two selectors. Dependencies' extras do not activate the target's selectors.

Sources also apply to transitive dependencies. Pip rejects conflicting source choices for the same package. Requested extras and dependency markers remain in effect, and the selected version must satisfy the original version requirement.

A direct reference in the target takes precedence over a source entry. An applicable source replaces a dependency's direct reference. Pip logs applied locations and warns about unused entries.

For cross-platform commands, source markers use the supplied Python version and known platform information. Pip reports an error if a marker needs information that the supplied platform tags cannot identify, such as a kernel release or one machine architecture across incompatible platforms.

## Disable sources globally or in CI

Source declarations can fetch code from outside your normal indexes. Use `--no-sources` to check published dependencies or deploy a source checkout without its development redirects. It ignores the entire table, including invalid entries.

Set the usual pip environment variable in CI:

```sh
export PIP_NO_SOURCES=true
python -m pip install .
```

Or put this in a user or global pip configuration file:

```ini
[global]
no-sources = true
```

`PIP_SOURCES` and the `sources` configuration key can select a project explicitly. `--no-sources` takes precedence over that selection. Pip's ordinary configuration precedence and `--isolated` behavior apply.

Sources never redirect build requirements, including requirements installed in isolated subprocesses or through the in-process build installer. Normal build isolation, TLS verification, authentication and hash checks continue to apply.
