import pytest

from pip._internal.models.link import Link
from pip._internal.resolution.resolvelib.factory import _has_conflicting_links


@pytest.mark.parametrize(
    "urls, conflict",
    [
        ([], False),
        (["https://example.invalid/dependency-1.whl"], False),
        (
            [
                "https://example.invalid/dependency-1.whl#egg=dependency",
                "https://example.invalid/dependency-1.whl#egg=other",
            ],
            False,
        ),
        (
            [
                "https://example.invalid/dependency-1.whl?a=1&b=2",
                "https://example.invalid/dependency-1.whl?b=2&a=1",
            ],
            False,
        ),
        (
            [
                "https://example.invalid/dependency-1.whl",
                "https://example.invalid/dependency-2.whl",
            ],
            True,
        ),
    ],
)
def test_constraint_link_domains(urls: list[str], conflict: bool) -> None:
    assert _has_conflicting_links(Link(url) for url in urls) == conflict
