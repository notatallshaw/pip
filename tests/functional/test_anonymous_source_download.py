from __future__ import annotations

import shutil
from hashlib import sha256
from pathlib import Path

import pytest

from tests.lib import PipTestEnvironment, create_basic_sdist_for_package
from tests.lib.server import MockServer, file_response


@pytest.fixture
def anonymous_archive(script: PipTestEnvironment) -> Path:
    archive = create_basic_sdist_for_package(
        script,
        "project",
        "1.0",
        extra_files={
            "pyproject.toml": (
                '[build-system]\nrequires = ["unavailable-build-backend"]\n'
                'build-backend = "backend"\nbackend-path = ["."]\n'
            ),
            "backend.py": 'raise RuntimeError("Unexpected backend execution")\n',
        },
    )
    return archive.rename(script.scratch_path / "snapshot.tar.gz")


@pytest.mark.parametrize("source", ["path", "uri"])
def test_download_anonymous_archive_bytes(
    script: PipTestEnvironment, anonymous_archive: Path, source: str
) -> None:
    contents = anonymous_archive.read_bytes()
    requirement = (
        str(anonymous_archive) if source == "path" else anonymous_archive.as_uri()
    )
    result = script.pip(
        "download",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--dest",
        "downloaded",
        requirement,
    )
    downloaded = script.scratch_path / "downloaded" / anonymous_archive.name
    assert downloaded.read_bytes() == contents
    assert anonymous_archive.read_bytes() == contents
    assert "Successfully downloaded snapshot.tar.gz" in result.stdout


@pytest.mark.parametrize("filename", ["snapshot.tar.gz", "artifact"])
def test_download_anonymous_http_archive_bytes(
    script: PipTestEnvironment,
    anonymous_archive: Path,
    mock_server: MockServer,
    filename: str,
) -> None:
    contents = anonymous_archive.read_bytes()
    mock_server.set_responses([file_response(anonymous_archive)])
    mock_server.start()
    url = f"http://{mock_server.host}:{mock_server.port}/{filename}"
    script.pip(
        "download",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--dest",
        "downloaded",
        url,
        allow_stderr_warning=(filename == "artifact"),
    )
    assert script.scratch_path.joinpath("downloaded", filename).read_bytes() == contents


@pytest.mark.parametrize("metadata_version", ["2.1", "2.6"])
def test_anonymous_archive_does_not_need_header_metadata(
    script: PipTestEnvironment, metadata_version: str
) -> None:
    archive = create_basic_sdist_for_package(
        script,
        "project",
        "1.0",
        extra_files={
            "PKG-INFO": (
                f"Metadata-Version: {metadata_version}\nName: project\nVersion: 1.0\n"
                "Requires-Python: >=99\nRequires-Dist: unused-dependency==99\n"
            ),
            "pyproject.toml": (
                '[build-system]\nrequires = ["unavailable-build-backend"]\n'
                'build-backend = "backend"\nbackend-path = ["."]\n'
            ),
            "backend.py": 'raise RuntimeError("Unexpected backend execution")\n',
        },
    ).rename(script.scratch_path / "snapshot.tar.gz")
    contents = archive.read_bytes()
    script.pip(
        "download",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--dest",
        "downloaded",
        archive,
    )
    assert (
        script.scratch_path.joinpath("downloaded/snapshot.tar.gz").read_bytes()
        == contents
    )


@pytest.mark.parametrize("guard", ["named", "multiple", "constraint", "deps", "python"])
def test_other_downloads_still_request_metadata(
    script: PipTestEnvironment, anonymous_archive: Path, guard: str
) -> None:
    flags = ["--no-deps", "--ignore-requires-python"]
    roots = [anonymous_archive.as_uri()]
    if guard == "named":
        roots = [f"project @ {anonymous_archive.as_uri()}"]
    elif guard == "multiple":
        other = script.scratch_path / "other.tar.gz"
        other.write_bytes(anonymous_archive.read_bytes())
        roots.append(other.as_uri())
    elif guard == "constraint":
        constraint = script.temporary_file("constraint.txt", "project<2\n")
        flags.extend(["--constraint", str(constraint)])
    elif guard == "deps":
        flags.remove("--no-deps")
    else:
        flags.remove("--ignore-requires-python")
    result = script.pip(
        "download",
        "--no-index",
        "--no-build-isolation",
        *flags,
        *roots,
        expect_error=True,
    )
    assert "Unexpected backend execution" in result.stderr


def test_anonymous_archive_supplied_hash(
    script: PipTestEnvironment, anonymous_archive: Path
) -> None:
    contents = anonymous_archive.read_bytes()
    requirements = script.temporary_file(
        "requirements.txt",
        f"{anonymous_archive.as_uri()} --hash=sha256:{sha256(contents).hexdigest()}\n",
    )
    script.pip(
        "download",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--require-hashes",
        "--dest",
        "downloaded",
        "--requirement",
        requirements,
    )
    assert (
        script.scratch_path.joinpath("downloaded/snapshot.tar.gz").read_bytes()
        == contents
    )


def test_anonymous_archive_hash_error_preserves_input(
    script: PipTestEnvironment, anonymous_archive: Path
) -> None:
    contents = anonymous_archive.read_bytes()
    result = script.pip(
        "download",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--dest",
        script.scratch_path,
        f"{anonymous_archive.as_uri()}#sha256={'0' * 64}",
        expect_error=True,
    )
    assert anonymous_archive.read_bytes() == contents
    assert "DO NOT MATCH THE HASHES" in result.stderr


def test_anonymous_archive_requires_a_supplied_hash(
    script: PipTestEnvironment, anonymous_archive: Path
) -> None:
    contents = anonymous_archive.read_bytes()
    result = script.pip(
        "download",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--require-hashes",
        "--dest",
        "downloaded",
        anonymous_archive,
        expect_error=True,
    )
    assert anonymous_archive.read_bytes() == contents
    assert anonymous_archive.as_uri() in result.stderr
    assert sha256(contents).hexdigest() in result.stderr
    assert "Unexpected backend execution" not in result.stderr


@pytest.mark.parametrize("invalid", ["archive", "project", "requires", "backend-path"])
def test_anonymous_download_validates_source(
    script: PipTestEnvironment, invalid: str
) -> None:
    if invalid == "archive":
        archive = script.scratch_path / "broken.tar.gz"
        archive.write_bytes(b"not an archive")
    elif invalid == "project":
        project = script.scratch_path / "empty"
        project.mkdir()
        project.joinpath("README").write_text("Not a Python project")
        archive = Path(
            shutil.make_archive(
                str(script.scratch_path / "nonproject"), "gztar", root_dir=project
            )
        )
    elif invalid == "requires":
        archive = create_basic_sdist_for_package(
            script,
            "project",
            "1.0",
            extra_files={
                "pyproject.toml": '[build-system]\nrequires = "not a list"\n',
            },
        ).rename(script.scratch_path / "invalid.tar.gz")
    else:
        archive = create_basic_sdist_for_package(
            script,
            "project",
            "1.0",
            extra_files={
                "pyproject.toml": (
                    '[build-system]\nrequires = []\nbuild-backend = "backend"\n'
                    'backend-path = ["../outside"]\n'
                ),
            },
        ).rename(script.scratch_path / "invalid.tar.gz")
    contents = archive.read_bytes()
    result = script.pip(
        "download",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--dest",
        "downloaded",
        archive,
        expect_error=True,
    )
    assert archive.read_bytes() == contents
    if invalid == "requires":
        assert "Traceback" not in result.stderr
        assert "build-system.requires" in result.stderr
    result.did_not_create(Path("scratch/downloaded") / archive.name)


@pytest.mark.parametrize("resolver", ["resolvelib", "legacy"])
def test_anonymous_archive_inactive_roots_and_constraints(
    script: PipTestEnvironment, anonymous_archive: Path, resolver: str
) -> None:
    script.environ["PIP_USE_DEPRECATED"] = (
        "legacy-resolver" if resolver == "legacy" else ""
    )
    requirements = script.temporary_file(
        "requirements.txt",
        f'{anonymous_archive.as_uri()}\nignored==1; python_version < "0"\n',
    )
    constraint = script.temporary_file(
        "constraint.txt", 'project<2; python_version < "0"\n'
    )
    result = script.pip(
        "download",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--dest",
        "downloaded",
        "--requirement",
        requirements,
        "--constraint",
        constraint,
        *(["--no-build-isolation"] if resolver == "legacy" else []),
        expect_error=resolver == "legacy",
    )
    assert "Ignoring ignored" in result.stdout
    if resolver == "legacy":
        assert "Unexpected backend execution" in result.stderr
        result.did_not_create(Path("scratch/downloaded/snapshot.tar.gz"))
    else:
        assert "Ignoring project" not in result.stdout
        assert result.stdout.index("Processing") < result.stdout.index(
            "Ignoring ignored"
        )
        result.did_create(Path("scratch/downloaded/snapshot.tar.gz"))


@pytest.mark.parametrize("isolated", [False, True])
def test_anonymous_download_honors_build_checks(
    script: PipTestEnvironment, isolated: bool
) -> None:
    events = script.scratch_path / "events"
    script.environ["PIP_TEST_BUILD_EVENTS"] = str(events)
    archive = create_basic_sdist_for_package(
        script,
        "project",
        "1.0",
        extra_files={
            "pyproject.toml": (
                '[build-system]\nrequires = []\nbuild-backend = "backend"\n'
                'backend-path = ["."]\n'
            ),
            "backend.py": (
                "import os\n"
                "def get_requires_for_build_wheel(config_settings=None):\n"
                '    with open(os.environ["PIP_TEST_BUILD_EVENTS"], "a") as f:\n'
                '        f.write("requirements\\n")\n'
                "    return []\n"
                "def prepare_metadata_for_build_wheel(*args, **kwargs):\n"
                '    raise RuntimeError("Unexpected metadata hook")\n'
                "def build_wheel(*args, **kwargs):\n"
                '    raise RuntimeError("Unexpected wheel build")\n'
            ),
        },
    ).rename(script.scratch_path / "checked.tar.gz")
    options = [] if isolated else ["--no-build-isolation"]
    result = script.pip(
        "download",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--check-build-dependencies",
        *options,
        "--dest",
        "downloaded",
        archive,
    )
    observed = events.read_text().splitlines() if events.exists() else []
    assert observed == (["requirements"] if isolated else [])
    result.did_create(Path("scratch/downloaded/checked.tar.gz"))


def test_anonymous_download_checks_declared_missing_build_requirement(
    script: PipTestEnvironment, anonymous_archive: Path
) -> None:
    result = script.pip(
        "download",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--check-build-dependencies",
        "--no-build-isolation",
        "--dest",
        "downloaded",
        anonymous_archive,
        expect_error=True,
    )
    assert "are missing" in result.stderr
    assert "Unexpected backend execution" not in result.stderr


@pytest.mark.parametrize("resolver", ["resolvelib", "legacy"])
def test_anonymous_download_preserves_inactive_constraint_validation(
    script: PipTestEnvironment, anonymous_archive: Path, resolver: str
) -> None:
    script.environ["_PIP_TEST_ENV"] = ""
    script.environ["PIP_USE_DEPRECATED"] = (
        "legacy-resolver" if resolver == "legacy" else ""
    )
    constraint = script.temporary_file(
        "constraint.txt", f'{anonymous_archive.as_uri()} ; python_version < "0"\n'
    )
    result = script.pip(
        "download",
        "--no-index",
        "--no-deps",
        "--ignore-requires-python",
        "--constraint",
        constraint,
        "--dest",
        "downloaded",
        anonymous_archive,
        *(["--no-build-isolation"] if resolver == "legacy" else []),
        expect_error=True,
        allow_stderr_warning=True,
    )
    if resolver == "legacy":
        assert "Unexpected backend execution" in result.stderr
        result.did_not_create(Path("scratch/downloaded/snapshot.tar.gz"))
    else:
        assert "Unnamed requirements are not allowed as constraints" in result.stderr


@pytest.mark.parametrize("raw_download", [False, True])
@pytest.mark.parametrize("invalid", ["project", "constraint"])
def test_marker_error_preserves_root_validation_order(
    script: PipTestEnvironment, raw_download: bool, invalid: str
) -> None:
    script.environ["_PIP_TEST_ENV"] = ""
    archive = create_basic_sdist_for_package(
        script,
        "project",
        "1.0",
        extra_files={
            "pyproject.toml": '[build-system]\nrequires = "invalid"\n',
        },
    ).rename(script.scratch_path / "snapshot.tar.gz")
    options = ["--ignore-requires-python"] if raw_download else []
    if invalid == "constraint":
        constraint = script.temporary_file(
            "constraint.txt", f'{archive.as_uri()} ; "test" in dependency_groups\n'
        )
        options.extend(["--constraint", str(constraint)])
        roots = [archive.as_uri()]
    else:
        roots = [
            archive.as_uri(),
            'other==1; "test" in dependency_groups',
        ]
    result = script.pip(
        "download",
        "--no-index",
        "--no-deps",
        "--no-build-isolation",
        *options,
        *roots,
        expect_error=True,
    )
    expected = (
        "Unnamed requirements are not allowed as constraints"
        if invalid == "constraint"
        else "build-system.requires"
    )
    assert expected in result.stderr
    assert "UndefinedEnvironmentName" not in result.stderr
