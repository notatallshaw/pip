from pathlib import Path

import pytest

from pip._internal.commands.download import DownloadCommand
from pip._internal.req.constructors import (
    install_req_from_editable,
    install_req_from_line,
)
from pip._internal.utils.packaging import get_requirement


@pytest.mark.parametrize("source", ["archive", "directory"])
def test_legacy_resolver_keeps_ordinary_metadata_preparation(
    tmp_path: Path, source: str
) -> None:
    command = DownloadCommand("download", "")
    options, _ = command.parse_args(["--no-deps", "--ignore-requires-python"])
    options.deprecated_features_enabled = ["legacy-resolver"]
    req = install_req_from_line(
        "https://example.invalid/snapshot.tar.gz"
        if source == "archive"
        else tmp_path.as_uri()
    )

    assert command._single_source_without_metadata([req], options) is None


@pytest.mark.parametrize("options", [[], ["--no-deps"], ["--ignore-requires-python"]])
def test_anonymous_download_needs_both_flags(options: list[str]) -> None:
    command = DownloadCommand("download", "")
    parsed, _ = command.parse_args(options)
    req = install_req_from_line("https://example.invalid/snapshot.tar.gz")
    assert command._single_source_without_metadata([req], parsed) is None


@pytest.mark.parametrize("other", ["named", "constraint", "second", "vcs"])
def test_anonymous_download_preserves_other_selection(other: str) -> None:
    command = DownloadCommand("download", "")
    options, _ = command.parse_args(["--no-deps", "--ignore-requires-python"])
    req = install_req_from_line("https://example.invalid/snapshot.tar.gz")
    reqs = [req]
    if other == "named":
        req.req = get_requirement("project @ https://example.invalid/snapshot.tar.gz")
    elif other == "constraint":
        reqs.append(install_req_from_line("project==1", constraint=True))
    elif other == "second":
        reqs.append(install_req_from_line("https://example.invalid/other.tar.gz"))
    elif other == "vcs":
        reqs = [install_req_from_line("git+https://example.invalid/project")]
    assert command._single_source_without_metadata(reqs, options) is None


def test_inactive_roots_do_not_require_metadata() -> None:
    command = DownloadCommand("download", "")
    options, _ = command.parse_args(["--no-deps", "--ignore-requires-python"])
    req = install_req_from_line("https://example.invalid/snapshot.tar.gz")
    ignored = install_req_from_line('ignored==1; python_version < "0"')
    selected = command._single_source_without_metadata([req, ignored], options)
    assert selected is req


@pytest.mark.parametrize(
    "marker", ['"test" in dependency_groups', 'os_name ~= "not-a-version"']
)
def test_marker_errors_leave_validation_to_resolver(marker: str) -> None:
    command = DownloadCommand("download", "")
    options, _ = command.parse_args(["--no-deps", "--ignore-requires-python"])
    req = install_req_from_line("https://example.invalid/snapshot.tar.gz")
    uncertain = install_req_from_line(f"other==1; {marker}")
    assert command._single_source_without_metadata([req, uncertain], options) is None


def test_anonymous_directory_does_not_require_metadata(tmp_path: Path) -> None:
    command = DownloadCommand("download", "")
    options, _ = command.parse_args(["--no-deps", "--ignore-requires-python"])
    req = install_req_from_line(tmp_path.as_uri())
    assert command._single_source_without_metadata([req], options) is req


def test_editable_directory_download_still_requires_metadata(tmp_path: Path) -> None:
    command = DownloadCommand("download", "")
    options, _ = command.parse_args(["--no-deps", "--ignore-requires-python"])
    req = install_req_from_editable(tmp_path.as_uri())
    assert command._single_source_without_metadata([req], options) is None
