"""Index-advertised hash constraints and archive verification."""

import hashlib
import json
from typing import TYPE_CHECKING
from zipfile import ZipFile

import pytest
import tomli_w

from pip._vendor.packaging.pylock import Package, PackageWheel, Pylock
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import Version

from tests.lib import (
    PipTestEnvironment,
    create_basic_sdist_for_package,
    create_basic_wheel_for_package,
)
from tests.lib.server import (
    file_response,
    json_index_page,
    make_mock_server,
    package_page,
    server_running,
    text_html_response,
)

if TYPE_CHECKING:
    from _typeshed.wsgi import StartResponse, WSGIEnvironment


@pytest.mark.parametrize("protocol", ["html", "json"])
@pytest.mark.parametrize(
    "advertised", ["match", "uppercase", "missing", "mismatch", "corrupt"]
)
def test_index_hash_constraint_admission(
    script: PipTestEnvironment, protocol: str, advertised: str
) -> None:
    wheel = create_basic_wheel_for_package(script, "base", "1")
    actual = hashlib.sha256(wheel.read_bytes()).hexdigest()
    approved = "0" * 64 if advertised == "corrupt" else actual
    digest = {
        "match": approved,
        "uppercase": approved.upper(),
        "corrupt": approved,
        "mismatch": "1" * 64,
        "missing": None,
    }[advertised]
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"base --index-hash=sha256:{approved}\n")
    url = f"/files/{wheel.name}"
    server = make_mock_server()
    if protocol == "html":
        page = package_page({wheel.name: url + (f"#sha256={digest}" if digest else "")})
    else:
        page = json_index_page(
            "base",
            [
                {
                    "filename": wheel.name,
                    "url": url,
                    "hashes": {"sha256": digest} if digest else {},
                },
            ],
        )
    server.mock.side_effect = [page, file_response(wheel)]
    with server_running(server):
        result = script.pip(
            "download",
            "--no-cache-dir",
            "--no-deps",
            "--index-url",
            f"http://{server.host}:{server.port}/simple",
            "--dest",
            script.scratch_path / "downloads",
            "--constraint",
            constraints,
            "base",
            expect_error=advertised not in ("match", "uppercase"),
        )

    paths = [call.args[0]["PATH_INFO"] for call in server.mock.call_args_list]
    if advertised in ("missing", "mismatch"):
        assert paths == ["/simple/base/"]
        assert "ResolutionImpossible" in result.stderr
    else:
        assert paths == ["/simple/base/", url]
        if advertised == "corrupt":
            assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr
        else:
            assert (script.scratch_path / "downloads" / wheel.name).exists()


def test_index_hash_constraints_do_not_enable_global_hash_checking(
    script: PipTestEnvironment,
) -> None:
    parent = create_basic_wheel_for_package(script, "parent", "1", depends=["child"])
    child = create_basic_wheel_for_package(script, "child", "1")
    digest = hashlib.sha256(child.read_bytes()).hexdigest()
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"child --index-hash=sha256:{digest}\n")
    page = script.scratch_path / "index.html"
    page.write_text(
        f'<a href="{parent.as_uri()}">{parent.name}</a>\n'
        f'<a href="{child.as_uri()}#sha256={digest}">{child.name}</a>\n'
    )
    result = script.pip_install_local("parent", "-c", constraints, find_links=page)

    result.assert_installed("parent", editable=False)
    result.assert_installed("child", editable=False)


@pytest.mark.parametrize("dry_run", [False, True])
def test_index_hash_constraint_metadata_then_corrupt_archive(
    script: PipTestEnvironment, dry_run: bool
) -> None:
    wheel = create_basic_wheel_for_package(script, "base", "1")
    with ZipFile(wheel) as archive:
        metadata = archive.read("base-1.dist-info/METADATA").decode()
    constraints = script.scratch_path / "constraints.txt"
    approved = "0" * 64
    constraints.write_text(f"base --index-hash=sha256:{approved}\n")
    url = f"/files/{wheel.name}"
    server = make_mock_server()
    server.mock.side_effect = [
        text_html_response(
            f'<a href="{url}#sha256={approved}" data-core-metadata="true">base</a>'
        ),
        text_html_response(metadata),
        file_response(wheel),
    ]
    with server_running(server):
        result = script.pip(
            "install",
            "--no-cache-dir",
            "--no-deps",
            "--index-url",
            f"http://{server.host}:{server.port}/simple",
            "--constraint",
            constraints,
            "base",
            *(
                ["--dry-run", "--report", str(script.scratch_path / "report.json")]
                if dry_run
                else []
            ),
            expect_error=not dry_run,
        )

    paths = [call.args[0]["PATH_INFO"] for call in server.mock.call_args_list]
    assert paths == ["/simple/base/", url + ".metadata"] + ([] if dry_run else [url])
    if not dry_run:
        assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr
    result.assert_not_installed("base")


@pytest.mark.parametrize("bad_hash", [None, "requirement", "constraint"])
@pytest.mark.parametrize("global_hashes", [False, True])
def test_index_and_requirement_hashes_are_independent(
    script: PipTestEnvironment,
    bad_hash: str | None,
    global_hashes: bool,
) -> None:
    wheel = create_basic_wheel_for_package(script, "base", "1")
    data = wheel.read_bytes()
    index_digest = hashlib.sha256(data).hexdigest()
    user_digest = hashlib.sha512(data).hexdigest()
    if bad_hash == "constraint":
        index_digest = "0" * 64
    if bad_hash == "requirement":
        user_digest = "0" * 128
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"base --index-hash=sha256:{index_digest}\n")
    requirements = script.scratch_path / "requirements.txt"
    requirements.write_text(f"base --hash=sha512:{user_digest}\n")
    page = script.scratch_path / "index.html"
    page.write_text(f'<a href="{wheel.as_uri()}#sha256={index_digest}">base</a>')
    result = script.pip_install_local(
        "--require-hashes" if global_hashes else "--no-require-hashes",
        "-r",
        requirements,
        "-c",
        constraints,
        find_links=page,
        expect_error=bad_hash is not None,
    )

    if bad_hash is None:
        result.assert_installed("base", editable=False)
    else:
        assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr
        result.assert_not_installed("base")


def test_index_hash_constraint_refuses_direct_requirement_before_download(
    script: PipTestEnvironment,
) -> None:
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"base --index-hash=sha256:{'0' * 64}\n")
    server = make_mock_server()
    server.mock.side_effect = AssertionError(
        "Direct requirements cannot use index hashes"
    )
    with server_running(server):
        result = script.pip(
            "install",
            "--no-index",
            "-c",
            constraints,
            f"base @ http://{server.host}:{server.port}/base-1-py3-none-any.whl",
            expect_error=True,
        )

    assert server.mock.call_count == 0
    result.assert_not_installed("base")


def test_index_hash_constraint_checks_download_directory(
    script: PipTestEnvironment,
) -> None:
    wheel = create_basic_wheel_for_package(script, "base", "1")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"base --index-hash=sha256:{digest}\n")
    destination = script.scratch_path / "downloads"
    destination.mkdir()
    cached = destination / wheel.name
    cached.write_bytes(b"corrupt previously downloaded wheel")
    url = f"/files/{wheel.name}"
    server = make_mock_server()
    server.mock.side_effect = [
        package_page({wheel.name: url + f"#sha256={digest}"}),
        file_response(wheel),
    ]
    with server_running(server):
        script.pip(
            "download",
            "--no-deps",
            "--no-cache-dir",
            "-c",
            constraints,
            "--dest",
            destination,
            "--index-url",
            f"http://{server.host}:{server.port}/simple",
            "base",
            allow_stderr_warning=True,
        )

    assert cached.read_bytes() == wheel.read_bytes()
    assert [call.args[0]["PATH_INFO"] for call in server.mock.call_args_list] == [
        "/simple/base/",
        url,
    ]


@pytest.mark.parametrize("provenance", ["match", "missing", "mismatch", "absent"])
def test_index_hash_constraint_reuses_checked_source_wheel_cache(
    script: PipTestEnvironment,
    provenance: str,
) -> None:
    source = create_basic_sdist_for_package(
        script, "base", "1", extra_files={"base/__init__.py": ""}
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"base --index-hash=sha256:{digest}\n")
    page = script.scratch_path / "index.html"
    page.write_text(f'<a href="{source.as_uri()}#sha256={digest}">base</a>')
    cache = script.scratch_path / "wheel-cache"
    args = ("--no-build-isolation", "--cache-dir", cache, "-c", constraints, "base")
    first = script.pip_install_local(*args, find_links=page)
    assert "Created wheel for base" in first.stdout
    script.pip("uninstall", "--yes", "base")

    origin = next(cache.rglob("origin.json"))
    if provenance == "absent":
        origin.unlink()
    elif provenance != "match":
        recorded = json.loads(origin.read_text())
        recorded["archive_info"] = (
            {"hashes": {"sha256": "0" * 64}} if provenance == "mismatch" else {}
        )
        origin.write_text(json.dumps(recorded))

    second = script.pip_install_local(
        *args,
        find_links=page,
        allow_stderr_warning=provenance != "match",
    )
    if provenance != "absent":
        assert "Using cached base" in second.stdout
    if provenance in ("missing", "mismatch"):
        assert "re-downloading source" in second.stderr
    if provenance != "match":
        assert "Created wheel for base" in second.stdout
    second.assert_installed("base", editable=False)
    assert json.loads(origin.read_text())["archive_info"]["hashes"] == {
        "sha256": digest
    }


@pytest.mark.parametrize("second_constraint", ["match", "disjoint", "corrupt"])
def test_index_hash_constraints_require_each_advertised_algorithm(
    script: PipTestEnvironment, second_constraint: str
) -> None:
    wheel = create_basic_wheel_for_package(script, "base", "1")
    data = wheel.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    sha512 = hashlib.sha512(data).hexdigest()
    advertised_sha512 = "0" * 128 if second_constraint == "corrupt" else sha512
    allowed_sha512 = sha512 if second_constraint == "disjoint" else advertised_sha512
    if second_constraint == "disjoint":
        advertised_sha512 = "0" * 128
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(
        f"base --index-hash=sha256:{sha256} --index-hash=sha256:{'1' * 64}\n"
        f"base --index-hash=sha512:{allowed_sha512}\n"
    )
    url = f"/files/{wheel.name}"
    server = make_mock_server()
    server.mock.side_effect = [
        json_index_page(
            "base",
            [
                {
                    "filename": wheel.name,
                    "url": url,
                    "hashes": {"sha256": sha256, "sha512": advertised_sha512},
                }
            ],
        ),
        file_response(wheel),
    ]
    with server_running(server):
        result = script.pip(
            "install",
            "--no-cache-dir",
            "--index-url",
            f"http://{server.host}:{server.port}/simple",
            "-c",
            constraints,
            "base",
            expect_error=second_constraint != "match",
        )

    paths = [call.args[0]["PATH_INFO"] for call in server.mock.call_args_list]
    if second_constraint == "disjoint":
        assert paths == ["/simple/base/"]
    else:
        assert paths == ["/simple/base/", url]
    if second_constraint == "match":
        result.assert_installed("base", editable=False)
    else:
        result.assert_not_installed("base")
        if second_constraint == "corrupt":
            assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr


def test_index_hash_constraint_backtracks_without_fetching_rejected_child(
    script: PipTestEnvironment,
) -> None:
    parent1 = create_basic_wheel_for_package(
        script, "parent", "1", depends=["child==1"]
    )
    parent2 = create_basic_wheel_for_package(
        script, "parent", "2", depends=["child==2"]
    )
    child1 = create_basic_wheel_for_package(script, "child", "1")
    digest = hashlib.sha256(child1.read_bytes()).hexdigest()
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"child --index-hash=sha256:{digest}\n")
    rejected_url = f"/files/child-2-py3-none-any.whl#sha256={'0' * 64}"
    routes = {
        "/simple/parent/": package_page(
            {
                parent1.name: f"/files/{parent1.name}",
                parent2.name: f"/files/{parent2.name}",
            }
        ),
        "/simple/child/": package_page(
            {
                child1.name: f"/files/{child1.name}#sha256={digest}",
                "child-2-py3-none-any.whl": rejected_url,
            }
        ),
        f"/files/{parent1.name}": file_response(parent1),
        f"/files/{parent2.name}": file_response(parent2),
        f"/files/{child1.name}": file_response(child1),
    }
    server = make_mock_server()
    server.mock.side_effect = lambda env, _: routes[env["PATH_INFO"]]
    with server_running(server):
        result = script.pip(
            "install",
            "--no-cache-dir",
            "--index-url",
            f"http://{server.host}:{server.port}/simple",
            "-c",
            constraints,
            "parent",
        )

    paths = [call.args[0]["PATH_INFO"] for call in server.mock.call_args_list]
    assert "/files/child-2-py3-none-any.whl" not in paths
    assert f"/files/{parent2.name}" in paths
    assert f"/files/{parent1.name}" in paths
    result.assert_installed("parent", editable=False)
    result.assert_installed("child", editable=False)
    assert "Successfully installed child-1 parent-1" in result.stdout


def test_index_hash_constraint_preserves_extras(script: PipTestEnvironment) -> None:
    wheel = create_basic_wheel_for_package(
        script, "base", "1", extras={"feature": ["child"]}
    )
    child = create_basic_wheel_for_package(script, "child", "1")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"base --index-hash=sha256:{digest}\n")
    page = script.scratch_path / "index.html"
    page.write_text(
        f'<a href="{wheel.as_uri()}#sha256={digest}">base</a>\n'
        f'<a href="{child.as_uri()}">child</a>\n'
    )
    result = script.pip_install_local(
        "base[feature]", "-c", constraints, find_links=page
    )

    result.assert_installed("base", editable=False)
    result.assert_installed("child", editable=False)


def test_index_hash_constraint_checks_index_for_installed_package(
    script: PipTestEnvironment,
) -> None:
    wheel = create_basic_wheel_for_package(script, "base", "1")
    script.pip_install_local(wheel)
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"base --index-hash=sha256:{'0' * 64}\n")
    server = make_mock_server()
    server.mock.side_effect = [package_page({wheel.name: f"/files/{wheel.name}"})]
    with server_running(server):
        script.pip(
            "install",
            "--index-url",
            f"http://{server.host}:{server.port}/simple",
            "-c",
            constraints,
            "base",
            expect_error=True,
        )

    assert [call.args[0]["PATH_INFO"] for call in server.mock.call_args_list] == [
        "/simple/base/"
    ]
    assert "Version: 1" in script.pip("show", "base").stdout


def test_index_hash_constraint_rejects_legacy_resolver(
    script: PipTestEnvironment,
) -> None:
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"base --index-hash=sha256:{'0' * 64}\n")
    result = script.pip_install_local(
        "--use-deprecated=legacy-resolver",
        "-c",
        constraints,
        "base",
        expect_error=True,
    )

    assert "--index-hash is not supported by the legacy resolver" in result.stderr
    result.assert_not_installed("base")


@pytest.mark.parametrize("corrupt_cache", [False, True])
def test_index_hash_constraint_verifies_http_cache(
    script: PipTestEnvironment,
    corrupt_cache: bool,
) -> None:
    wheel = create_basic_wheel_for_package(script, "base", "1")
    data = wheel.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"base --index-hash=sha256:{digest}\n")
    cache = script.scratch_path / "http-cache"
    url = f"/files/{wheel.name}"
    page = package_page({wheel.name: url + f"#sha256={digest}"})

    def cached_wheel(
        environ: "WSGIEnvironment", start_response: "StartResponse"
    ) -> list[bytes]:
        start_response(
            "200 OK",
            [
                ("Content-Type", "application/octet-stream"),
                ("Content-Length", str(len(data))),
                ("Cache-Control", "public, max-age=3600"),
            ],
        )
        return [data]

    server = make_mock_server()
    server.mock.side_effect = [page, cached_wheel, page]
    args = (
        "download",
        "--retries",
        "0",
        "--cache-dir",
        cache,
        "--index-url",
        f"http://{server.host}:{server.port}/simple",
        "--trusted-host",
        server.host,
        "-c",
        constraints,
        "base",
    )
    with server_running(server):
        script.pip(*args, "--dest", script.scratch_path / "first")
        if corrupt_cache:
            bodies = [
                body for body in cache.rglob("*.body") if body.read_bytes() == data
            ]
            assert len(bodies) == 1
            # Preserve length so the transport accepts the cached response.
            bodies[0].write_bytes(b"x" * len(data))
        result = script.pip(
            *args,
            "--dest",
            script.scratch_path / "second",
            expect_error=corrupt_cache,
        )

    assert [call.args[0]["PATH_INFO"] for call in server.mock.call_args_list] == [
        "/simple/base/",
        url,
        "/simple/base/",
    ]
    if corrupt_cache:
        assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr
    else:
        assert (script.scratch_path / "second" / wheel.name).read_bytes() == data


def test_index_hash_constraint_filters_extra_index_files(
    script: PipTestEnvironment,
) -> None:
    wheel = create_basic_wheel_for_package(script, "base", "1")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"base --index-hash=sha256:{digest}\n")
    url = f"/files/{wheel.name}"
    server = make_mock_server()
    server.mock.side_effect = [
        package_page({wheel.name: url + f"#sha256={digest}"}),
        package_page({"base-2-py3-none-any.whl": "/files/base-2-py3-none-any.whl"}),
        file_response(wheel),
    ]
    with server_running(server):
        result = script.pip(
            "install",
            "--no-cache-dir",
            "--index-url",
            f"http://{server.host}:{server.port}/primary",
            "--extra-index-url",
            f"http://{server.host}:{server.port}/extra",
            "-c",
            constraints,
            "base",
        )

    assert [call.args[0]["PATH_INFO"] for call in server.mock.call_args_list] == [
        "/primary/base/",
        "/extra/base/",
        url,
    ]
    result.assert_installed("base", editable=False)


@pytest.mark.parametrize("source", ["find-links", "pylock"])
def test_index_hash_constraint_requires_an_index_advertisement(
    script: PipTestEnvironment,
    source: str,
) -> None:
    wheel = create_basic_wheel_for_package(script, "base", "1")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"base --index-hash=sha256:{digest}\n")
    server = make_mock_server()
    server.mock.return_value = file_response(wheel)
    url = f"http://{server.host}:{server.port}/{wheel.name}#sha256={digest}"
    if source == "find-links":
        args = ["--find-links", url, "base"]
    else:
        lock = Pylock(
            lock_version=Version("1.0"),
            created_by="pip",
            packages=[
                Package(
                    name=canonicalize_name("base"),
                    version=Version("1"),
                    wheels=[PackageWheel(url=url, hashes={"sha256": digest})],
                )
            ],
        )
        path = script.scratch_path / "pylock.toml"
        path.write_text(tomli_w.dumps(lock.to_dict()), encoding="utf-8")
        args = ["-r", str(path)]
    with server_running(server):
        result = script.pip(
            "install", "--no-index", "-c", constraints, *args, expect_error=True
        )

    requests = [call.args[0] for call in server.mock.call_args_list]
    assert not any(request["REQUEST_METHOD"] == "GET" for request in requests)
    result.assert_not_installed("base")


def test_index_hash_constraint_cache_records_checked_source_hashes(
    script: PipTestEnvironment,
) -> None:
    source = create_basic_sdist_for_package(
        script, "base", "1", extra_files={"base/__init__.py": ""}
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    wrong_sha512 = "0" * 128
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"base --index-hash=sha256:{digest}\n")
    cache = script.scratch_path / "wheel-cache"
    server = make_mock_server()
    server.mock.return_value = json_index_page(
        "base",
        [
            {
                "filename": source.name,
                "url": source.as_uri(),
                "hashes": {"sha512": wrong_sha512, "sha256": digest},
            }
        ],
    )
    args = (
        "install",
        "--no-build-isolation",
        "--cache-dir",
        cache,
        "--index-url",
        f"http://{server.host}:{server.port}/simple",
        "-c",
        constraints,
        "base",
    )
    with server_running(server):
        script.pip(*args)
        origin = json.loads(next(cache.rglob("origin.json")).read_text())
        script.pip("uninstall", "--yes", "base")
        constraints.write_text(f"base --index-hash=sha512:{wrong_sha512}\n")
        result = script.pip(*args, expect_error=True)

    assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr
    result.assert_not_installed("base")
    assert origin["archive_info"]["hashes"] == {"sha256": digest}


def test_index_hash_constraint_missing_origin_does_not_trust_cache_key(
    script: PipTestEnvironment,
) -> None:
    source = create_basic_sdist_for_package(
        script, "base", "1", extra_files={"base/__init__.py": ""}
    )
    actual = hashlib.sha256(source.read_bytes()).hexdigest()
    advertised = "0" * 64
    constraints = script.scratch_path / "constraints.txt"
    constraints.write_text(f"base --hash=sha256:{actual}\n")
    cache = script.scratch_path / "wheel-cache"
    server = make_mock_server()
    server.mock.return_value = json_index_page(
        "base",
        [
            {
                "filename": source.name,
                "url": source.as_uri(),
                "hashes": {"sha256": advertised},
            }
        ],
    )
    args = (
        "install",
        "--no-build-isolation",
        "--no-require-hashes",
        "--cache-dir",
        cache,
        "--index-url",
        f"http://{server.host}:{server.port}/simple",
        "-c",
        constraints,
        "base",
    )
    with server_running(server):
        script.pip(*args)
        origin = next(cache.rglob("origin.json"))
        assert json.loads(origin.read_text())["archive_info"]["hashes"] == {
            "sha256": actual
        }
        script.pip("uninstall", "--yes", "base")
        origin.unlink()
        constraints.write_text(f"base --index-hash=sha256:{advertised}\n")
        result = script.pip(*args, expect_error=True)

    assert "THESE PACKAGES DO NOT MATCH THE HASHES" in result.stderr
    result.assert_not_installed("base")
