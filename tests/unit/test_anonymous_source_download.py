from pathlib import Path

import pytest

from pip._internal.commands.download import DownloadCommand
from pip._internal.req.constructors import install_req_from_line
from pip._internal.utils.packaging import get_requirement


def test_legacy_resolver_keeps_ordinary_metadata_preparation() -> None:
    command = DownloadCommand("download", "")
    options, _ = command.parse_args(["--no-deps", "--ignore-requires-python"])
    options.deprecated_features_enabled = ["legacy-resolver"]
    req = install_req_from_line("https://example.invalid/snapshot.tar.gz")

    assert command._single_archive_without_metadata([req], options) is None


@pytest.mark.parametrize("options", [[], ["--no-deps"], ["--ignore-requires-python"]])
def test_anonymous_download_needs_both_flags(options: list[str]) -> None:
    command = DownloadCommand("download", "")
    parsed, _ = command.parse_args(options)
    req = install_req_from_line("https://example.invalid/snapshot.tar.gz")
    assert command._single_archive_without_metadata([req], parsed) is None


@pytest.mark.parametrize("other", ["named", "constraint", "second", "vcs", "directory"])
def test_anonymous_download_preserves_other_selection(
    tmp_path: Path, other: str
) -> None:
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
    else:
        reqs = [install_req_from_line(tmp_path.as_uri())]
    assert command._single_archive_without_metadata(reqs, options) is None


def test_inactive_roots_do_not_require_metadata() -> None:
    command = DownloadCommand("download", "")
    options, _ = command.parse_args(["--no-deps", "--ignore-requires-python"])
    req = install_req_from_line("https://example.invalid/snapshot.tar.gz")
    ignored = install_req_from_line('ignored==1; python_version < "0"')
    selected = command._single_archive_without_metadata([req, ignored], options)
    assert selected is req


@pytest.mark.parametrize(
    "marker", ['"test" in dependency_groups', 'os_name ~= "not-a-version"']
)
def test_marker_errors_leave_validation_to_resolver(marker: str) -> None:
    command = DownloadCommand("download", "")
    options, _ = command.parse_args(["--no-deps", "--ignore-requires-python"])
    req = install_req_from_line("https://example.invalid/snapshot.tar.gz")
    uncertain = install_req_from_line(f"other==1; {marker}")
    assert command._single_archive_without_metadata([req, uncertain], options) is None
