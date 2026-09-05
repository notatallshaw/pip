import pytest

from pip._internal.models.link import Link, links_equivalent
from pip._internal.resolution.nab import host as host_module
from pip._internal.resolution.nab.factory import Factory
from pip._internal.resolution.nab.host import NativeHost
from pip._internal.resolution.nab.provider import PipProvider


@pytest.mark.parametrize(
    "url,alias",
    [
        ("https://example.org/a.whl", "https://user:pass@example.org/a.whl"),
        ("https://example.org/a.whl", "https://example.org/a.whl#egg=a"),
        ("https://example.org/a.whl?a=1&b=2", "https://example.org/a.whl?b=2&a=1"),
        ("file:///a.whl", "file://localhost/a.whl"),
    ],
)
def test_link_source_reuses_exact_and_equivalent_links(
    factory: Factory,
    provider: PipProvider,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    alias: str,
) -> None:
    comparisons: list[tuple[Link, Link]] = []

    def compare(left: Link, right: Link) -> bool:
        """Count equivalence checks without changing link semantics."""
        comparisons.append((left, right))
        return links_equivalent(left, right)

    monkeypatch.setattr(host_module, "links_equivalent", compare)
    host = NativeHost(factory, provider)
    first = Link(url)
    assert host._link_source(first, False) == "link:0"
    assert host._link_source(Link(url, hashes={"sha256": "abc"}), False) == "link:0"
    assert comparisons == []

    assert host._link_source(Link("https://example.org/b.whl"), False) == "link:1"
    comparisons.clear()
    assert host._link_source(Link(alias), False) == "link:0"
    assert len(comparisons) == 1
    assert host._link_source(Link(alias), False) == "link:0"
    assert len(comparisons) == 1
    assert len(host.sources) == host.availability_generation() == 2
    assert host.sources[0] == (first, False)


def test_link_source_separates_editable_modes(
    factory: Factory, provider: PipProvider
) -> None:
    host = NativeHost(factory, provider)
    ordinary = Link("https://example.org/a.tar.gz")
    alias = Link("https://example.org/a.tar.gz#egg=a")

    assert host._link_source(ordinary, False) == "link:0"
    assert host._link_source(alias, True) == "link:1"
    assert host._link_source(ordinary, True) == "link:1"
    assert host._link_source(alias, False) == "link:0"
    assert host._link_source(ordinary, False) == "link:0"
    assert host._link_source(alias, True) == "link:1"
    assert host.sources == [(ordinary, False), (alias, True)]
    assert host.availability_generation() == 2


def test_link_sources_belong_to_each_host(
    factory: Factory, provider: PipProvider
) -> None:
    first = NativeHost(factory, provider)
    second = NativeHost(factory, provider)
    a = Link("https://example.org/a.whl")
    b = Link("https://example.org/b.whl")

    assert first._link_source(a, False) == "link:0"
    assert first._link_source(b, False) == "link:1"
    assert second._link_source(b, False) == "link:0"
    assert second._link_source(a, False) == "link:1"
    assert first._link_source(a, False) == "link:0"
    assert first.availability_generation() == second.availability_generation() == 2


@pytest.mark.parametrize("fragment", ["sha256=abc", "subdirectory=a"])
def test_link_source_preserves_significant_fragments(
    factory: Factory, provider: PipProvider, fragment: str
) -> None:
    host = NativeHost(factory, provider)
    url = "https://example.org/a.tar.gz"
    plain = Link(url, hashes={"sha256": "abc"})
    fragmented = Link(f"{url}#{fragment}")

    assert host._link_source(plain, False) == "link:0"
    assert host._link_source(fragmented, False) == "link:1"
    assert host._link_source(Link(url), False) == "link:0"
    assert host._link_source(Link(f"{url}#{fragment}"), False) == "link:1"
    assert host.availability_generation() == 2
