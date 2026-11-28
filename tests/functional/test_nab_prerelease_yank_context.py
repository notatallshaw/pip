import pytest

from tests.lib import PipTestEnvironment
from tests.lib.resolver import yanked_listing


@pytest.mark.parametrize("unrelated", [False, True])
def test_unmatched_yank_cannot_admit_a_prerelease(
    script: PipTestEnvironment, unrelated: bool
) -> None:
    packages = [("dep", "4", ["missing==1"]), ("dep", "4rc1", [])]
    if unrelated:
        packages += [("other", "2", []), ("other", "1", [])]
        withdrawn = {("other", "2")}
        roots = ["dep>=3", "other"]
    else:
        packages += [("dep", "2", [])]
        withdrawn = {("dep", "2")}
        roots = ["dep>=3"]
    listing = yanked_listing(script, packages, withdrawn=withdrawn)

    script.pip_install_local(*roots, find_links=listing, expect_error=True)
    script.assert_not_installed("dep")
