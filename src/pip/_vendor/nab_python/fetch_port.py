"""The fetch interface a host supplies so nab's Provider can run.

nab ships one implementation, :class:`~nab_python.fetch.FetchCoordinator`, which
overlaps index I/O on a background thread.  An embedding application supplies its
own: pip, for instance, already owns a package finder, a session and a download
path, and wants nab's resolution logic over them rather than nab's networking.

Two things make that possible and both are declared here rather than assumed.

Nothing in this port is async.  A request submits work and returns a
:class:`Waitable`; the provider blocks on ``wait()`` only where it needs the
answer.  nab's coordinator returns a :class:`threading.Event` that its fetcher
thread sets.  A synchronous host may return an already-set event, or an object
whose ``wait()`` performs the fetch, which turns the provider's fire-and-forget
prefetches into work that is only paid for when it is read.

The store is deliberately not a protocol.  :class:`~nab_python.store.InMemoryIndex`
is stdlib-only, has no network and no filesystem, and is the one object both the
writer and the reader agree on, so a host uses it directly.  A protocol with one
implementation on both sides of a port is a cost with no benefit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .store import InMemoryIndex

__all__ = ["FetchPort", "Waitable"]


class Waitable(Protocol):
    """What a request hands back.  :class:`threading.Event` satisfies it."""

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the requested data has landed in the store.

        Returns whether it landed, as :meth:`threading.Event.wait` does.  The
        provider ignores the result and reads the store, so a host that cannot
        fail may always return ``True``.
        """
        ...


class FetchPort(Protocol):
    """The fetch surface :class:`~nab_python.provider.Provider` consumes.

    A member's audience is stated on it, because the port has two kinds of
    member and mistaking one for the other costs a host real work.  Every host
    needs the listing and metadata requests and the store.  The rest exist for
    the sources nab's own CLI accepts, and a host that passes no local
    directories, no VCS references and no archive URLs, and never builds a
    remote sdist, can raise from them.  They still have to be present: Python
    resolves an attribute when it is called, but a type checker resolves it
    always.

    ``indexes`` is not here.  It is read at one site inside nab's own engine,
    behind a lock builder that a host resolving without a lock never
    constructs, so it belongs to nab's coordinator rather than to this port.
    """

    @property
    def index(self) -> InMemoryIndex:
        """The store this port writes to and the provider reads.

        Read-only: the provider never replaces it, and one store is shared by
        every per-target provider of a run.
        """
        ...

    @property
    def offline(self) -> bool:
        """Whether this run may read a cache only, never the network.

        For nab's CLI, the ``--offline`` flag.  A host with no offline mode
        returns ``False``.
        """
        ...

    def request_listing(self, package: str) -> Waitable:
        """Request every artifact of ``package``, for every host.

        The result lands under :meth:`~nab_python.store.InMemoryIndex.store_listing`,
        with the serving index recorded under ``store_listing_index``.  A failure
        lands under ``store_listing_error`` and the provider re-raises it.
        """
        ...

    def request_metadata(
        self,
        package: str,
        version: str,
        url: str,
        metadata_hash: tuple[str, str] | None = None,
    ) -> Waitable:
        """Request the METADATA at ``url`` for ``(package, version)``, for every host.

        ``url`` names one artifact, because sibling wheels of a version can
        declare different dependencies, and the result must be stored under that
        same URL.  ``metadata_hash`` is the digest the index published for it.
        """
        ...

    def request_metadata_batch(
        self, items: list[tuple[str, str, str, tuple[str, str] | None]]
    ) -> Sequence[tuple[str, str, Waitable]]:
        """Request several sidecars at once, for every host.

        Each item is ``(package, version, url, metadata_hash)``.  The provider
        submits speculatively and often does not read most of the batch, so a
        host with no concurrency to gain should make submission cheap rather
        than fetch inline.  The returned rows are ``(package, version,
        waitable)`` in the order submitted.
        """
        ...

    def request_range_metadata(
        self,
        package: str,
        version: str,
        wheel_url: str,
        wheel_hashes: tuple[tuple[str, str], ...] = (),
    ) -> Waitable:
        """Recover a sidecar-less wheel's METADATA by reading part of the wheel.

        Only reached for a wheel the index published without a PEP 658 sidecar.
        A host whose own metadata path already covers that case, or that only
        supplies artifacts carrying a sidecar, never reaches it.
        """
        ...

    def request_sdist(
        self,
        package: str,
        version: str,
        url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> Waitable:
        """Extract an sdist's PKG-INFO, for a host that reads sdist metadata.

        A host that instead builds sdists to learn their metadata serves this
        through its own metadata path and never reaches it.
        """
        ...

    def request_sdist_archive(
        self,
        package: str,
        version: str,
        url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> Waitable:
        """Fetch an sdist's raw bytes, for a host that builds remote sdists.

        The bytes land under
        :meth:`~nab_python.store.InMemoryIndex.store_sdist_archive`.  A host that
        owns building above the port never reaches it.
        """
        ...

    def request_direct_archive(self, package: str, version: str, url: str) -> Waitable:
        """Fetch an archive named by URL, for a host that accepts archive sources.

        The URL is declared independently of any index, so its own scheme decides
        how it is read.  A host that owns direct URLs above the port never
        reaches it.
        """
        ...
