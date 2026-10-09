"""Enforce input checksums independently of an archive's source identity."""

import hashlib
from itertools import repeat
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.functional.test_nab_preparation_reuse import tracked_install
from tests.lib import PipTestEnvironment
from tests.lib.server import MockServer
from tests.lib.wheel import make_wheel

if TYPE_CHECKING:
    from _typeshed.wsgi import StartResponse, WSGIApplication, WSGIEnvironment


def _range_response(content: bytes) -> "WSGIApplication":
    """Serve a wheel with HTTP ranges for metadata-only preparation."""

    def respond(
        environ: "WSGIEnvironment", start_response: "StartResponse"
    ) -> list[bytes]:
        body = content
        status = "200 OK"
        headers = [
            ("Accept-Ranges", "bytes"),
            ("Content-Type", "application/octet-stream"),
        ]
        requested = environ.get("HTTP_RANGE")
        if isinstance(requested, str):
            start, end = requested.removeprefix("bytes=").split("-", 1)
            if start:
                first = int(start)
                last = min(int(end) if end else len(content) - 1, len(content) - 1)
            else:
                first, last = max(0, len(content) - int(end)), len(content) - 1
            body = content[first : last + 1]
            status = "206 Partial Content"
            headers.append(("Content-Range", f"bytes {first}-{last}/{len(content)}"))
        headers.append(("Content-Length", str(len(body))))
        start_response(status, headers)
        return [] if environ["REQUEST_METHOD"] == "HEAD" else [body]

    return respond


@pytest.mark.parametrize("correct_hash", [False, True])
def test_fragment_hashes_allow_metadata_only_downloads(
    script: PipTestEnvironment, mock_server: MockServer, *, correct_hash: bool
) -> None:
    supplied = Path(make_wheel("dep", "1").save_to_dir(script.scratch_path))
    content = supplied.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    wanted = digest if correct_hash else "0" * 64
    mock_server.set_responses(repeat(_range_response(content)))
    mock_server.start()
    source = f"http://{mock_server.host}:{mock_server.port}/{supplied.name}"
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"dep @ {source}#sha256={wanted}\n")

    result = script.pip(
        "install",
        "--no-index",
        "--no-cache-dir",
        "--use-feature=fast-deps",
        "-c",
        str(constraints),
        f"dep @ {source}#sha256={digest}",
        expect_error=not correct_hash,
        allow_stderr_warning=True,
    )
    mock_server.stop()

    assert any(request.get("HTTP_RANGE") for request in mock_server.get_requests())
    if correct_hash:
        script.assert_installed(dep="1")
    else:
        assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr
        script.assert_not_installed("dep")


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("correct_hash", [False, True])
@pytest.mark.parametrize("root_hash", [False, True])
def test_constraint_url_fragment_hash_is_checked(
    script: PipTestEnvironment, *, named: bool, correct_hash: bool, root_hash: bool
) -> None:
    supplied = Path(make_wheel("dep", "1").save_to_dir(script.scratch_path))
    digest = hashlib.sha256(supplied.read_bytes()).hexdigest()
    wanted = digest if correct_hash else "0" * 64
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"dep @ {supplied.as_uri()}#sha256={wanted}\n")
    source = supplied.as_uri() + (f"#sha256={digest}" if root_hash else "")
    root = f"dep @ {source}" if named else source

    result = tracked_install(
        script,
        root,
        options=("-c", str(constraints)),
        expect_error=not correct_hash,
    )

    if correct_hash:
        script.assert_installed(dep="1")
        assert "NATIVE True" not in result.stdout
        assert "NATIVE False" not in result.stdout
        script.pip("check")
    else:
        assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr
        script.assert_not_installed("dep")


@pytest.mark.parametrize("correct_hash", [False, True])
def test_root_fragment_does_not_widen_input_checksum_intersection(
    script: PipTestEnvironment, *, correct_hash: bool
) -> None:
    supplied = Path(make_wheel("dep", "1").save_to_dir(script.scratch_path))
    content = supplied.read_bytes()
    sha256 = hashlib.sha256(content).hexdigest()
    sha512 = hashlib.sha512(content).hexdigest()
    wrong = "0" * 128
    requirements = script.scratch_path / "requirements.txt"
    requirements.write_text(
        f"dep @ {supplied.as_uri()}#sha256={sha256} "
        f"--hash=sha512:{sha512} --hash=sha512:{wrong}\n"
    )
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(
        f"dep @ {supplied.as_uri()}#sha512={sha512 if correct_hash else wrong}\n"
    )

    result = tracked_install(
        script,
        "-r",
        str(requirements),
        options=("-c", str(constraints)),
        expect_error=not correct_hash,
    )

    if correct_hash:
        script.assert_installed(dep="1")
    else:
        assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr
        script.assert_not_installed("dep")


def test_disjoint_input_fragments_reject_the_archive(
    script: PipTestEnvironment,
) -> None:
    supplied = Path(make_wheel("dep", "1").save_to_dir(script.scratch_path))
    digest = hashlib.sha256(supplied.read_bytes()).hexdigest()
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(
        f"dep @ {supplied.as_uri()}#sha256={digest}\n"
        f"dep @ {supplied.as_uri()}#sha256={'0' * 64}\n"
    )

    result = tracked_install(
        script,
        f"dep @ {supplied.as_uri()}",
        options=("-c", str(constraints)),
        expect_error=True,
    )

    assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr
    script.assert_not_installed("dep")


def test_input_checksum_intersection_ignores_hexadecimal_case(
    script: PipTestEnvironment,
) -> None:
    supplied = Path(make_wheel("dep", "1").save_to_dir(script.scratch_path))
    digest = hashlib.sha256(supplied.read_bytes()).hexdigest()
    requirements = script.scratch_path / "requirements.txt"
    requirements.write_text(
        f"dep @ {supplied.as_uri()}#sha256={digest} --hash=sha256:{digest.upper()}\n"
    )
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"dep @ {supplied.as_uri()}#sha256={digest}\n")

    tracked_install(script, "-r", str(requirements), options=("-c", str(constraints)))

    script.assert_installed(dep="1")
    script.pip("check")


@pytest.mark.parametrize("correct_hash", [False, True])
def test_selected_dependency_fragment_is_checked(
    script: PipTestEnvironment, *, correct_hash: bool
) -> None:
    supplied = Path(make_wheel("dep", "1").save_to_dir(script.scratch_path))
    digest = hashlib.sha256(supplied.read_bytes()).hexdigest()
    wanted = digest if correct_hash else "0" * 64
    metadata = "Metadata-Version: 2.1\nName: app\nVersion: 1\n"
    metadata += f"Requires-Dist: dep @ {supplied.as_uri()}#sha256={wanted}\n"
    make_wheel("app", "1", metadata=metadata).save_to_dir(script.scratch_path)

    result = tracked_install(
        script,
        "app",
        f"dep @ {supplied.as_uri()}",
        expect_error=not correct_hash,
    )

    if correct_hash:
        script.assert_installed(app="1", dep="1")
        assert "NATIVE True" not in result.stdout
        assert "NATIVE False" not in result.stdout
        script.pip("check")
    else:
        assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr
        script.assert_not_installed("app", "dep")


def test_shared_fragment_does_not_repeat_the_archive_checksum(
    script: PipTestEnvironment,
) -> None:
    supplied = Path(make_wheel("dep", "1").save_to_dir(script.scratch_path))
    digest = hashlib.sha256(supplied.read_bytes()).hexdigest()
    for name in ["app", "other"]:
        metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: 1\n"
        metadata += f"Requires-Dist: dep @ {supplied.as_uri()}#sha256={digest}\n"
        make_wheel(name, "1", metadata=metadata).save_to_dir(script.scratch_path)
    probe = script.scratch_path / "checksum_probe.py"
    probe.write_text(
        "import sys\n"
        "from pip._internal.cli.main import main\n"
        "from pip._internal.utils.hashes import Hashes\n"
        "archive = sys.argv.pop(1)\n"
        "original = Hashes.check_against_path\n"
        "def checked(self, path):\n"
        "    if path == archive and self.digest_count:\n"
        "        print('ARCHIVE CHECK')\n"
        "    return original(self, path)\n"
        "Hashes.check_against_path = checked\n"
        "sys.exit(main())\n"
    )

    result = script.run(
        "python",
        str(probe),
        str(supplied),
        "install",
        "--no-index",
        "--find-links",
        str(script.scratch_path),
        "app",
        "other",
        f"dep @ {supplied.as_uri()}",
    )

    script.assert_installed(app="1", other="1", dep="1")
    assert result.stdout.count("ARCHIVE CHECK") == 1
    script.pip("check")
