from collections.abc import Iterator, Mapping

import pytest

from pip._vendor.nab_resolver.candidate_provider import CandidateRequirement

from pip._internal.models.link import Link, links_equivalent
from pip._internal.resolution.nab import host as host_module
from pip._internal.resolution.nab.factory import Factory
from pip._internal.resolution.nab.host import NativeHost
from pip._internal.resolution.nab.provider import PipProvider
from pip._internal.resolution.nab.ranges import CandidateKey, CandidateRange


class TrackedDeclarations(
    Mapping[str, tuple[CandidateRequirement[str, CandidateKey], ...]]
):
    """Record which declaration groups a real native query consumes."""

    def __init__(
        self, values: dict[str, tuple[CandidateRequirement[str, CandidateKey], ...]]
    ) -> None:
        self._values = values
        self.reads: list[str] = []

    def __getitem__(
        self, key: str
    ) -> tuple[CandidateRequirement[str, CandidateKey], ...]:
        self.reads.append(key)
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def test_native_query_does_not_translate_unrelated_requirements(
    factory: Factory, provider: PipProvider
) -> None:
    host = NativeHost(factory, provider)
    requirements = {
        name: tuple(factory.make_requirements_from_spec(name, comes_from=None))
        for name in ("simplewheel", "simple")
    }
    declarations = TrackedDeclarations(
        {
            name: tuple(host.bind(req) for req in group)
            for name, group in requirements.items()
        }
    )

    selected = list(
        host.iter_candidates("simplewheel", CandidateRange.full(), declarations)
    )

    assert selected
    assert [host_module.native_candidate(candidate) for candidate in selected] == list(
        provider.find_matches("simplewheel", requirements)
    )
    assert set(declarations.reads) == {"simplewheel"}


def test_native_requirement_view_retains_all_keys_and_original_objects(
    factory: Factory, provider: PipProvider
) -> None:
    host = NativeHost(factory, provider)
    (requirement,) = factory.make_requirements_from_spec("simplewheel", comes_from=None)
    declarations = TrackedDeclarations(
        {"simplewheel": (host.bind(requirement),), "empty": ()}
    )
    native = host_module._NativeRequirements(declarations)

    assert len(native) == 2
    assert list(native) == ["simplewheel", "empty"]
    assert native["simplewheel"] == (requirement,)
    assert native["simplewheel"][0] is requirement
    assert declarations.reads == ["simplewheel"]
    assert dict(native) == {"simplewheel": (requirement,), "empty": ()}
    assert native.get("missing") is None


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
