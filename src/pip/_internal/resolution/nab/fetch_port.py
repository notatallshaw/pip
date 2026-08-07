"""pip's implementation of nab's ``FetchPort``.

nab's ``Provider`` reaches its index through a fetch port: nine members
that answer "list this package" and "read this artifact's metadata", plus
the shared ``InMemoryIndex`` both sides write into. nab's own coordinator
implements the port by submitting the work to a background asyncio loop.
This one implements it by calling pip's ``PackageFinder``, ``Factory`` and
``RequirementPreparer``, in the calling thread.

Two properties make that possible without asyncio.

Every request hands back a *lazy* waitable: the work is recorded and runs
inside ``wait()``. nab's provider fires prefetches it may never read (up to
``PREFETCH_BATCH`` per root scan, ``DEEP_PREFETCH_COUNT`` on the walk-ahead
path, one per transitive best), on the assumption that a request is a queue
put. Running those inline would make pip download metadata it never looks
at. Recording them costs a dict insert and only a real ``wait()`` pays.

Requests dedupe on ``(package, url)``, never on the version. nab spells a
version two ways on the metadata path: a prefetch passes the raw string off
the filename and the authoritative read passes ``str()`` of the parsed
``Version``. Under nab's coordinator that split costs a queue round trip;
here it would cost a second prepare.

What pip supplies per nab ``DistFile`` field:

===================  =========================================================
``filename``         ``Link.filename``
``url``              ``Link.url``
``version``          ``InstallationCandidate.version``, already parsed by pip
``requires_python``  ``None``. pip's ``LinkEvaluator`` already applied it
``has_metadata``     ``True`` for every wheel, so nab's ladder stops at the
                     sidecar rung and this port answers it. A ``False`` here
                     would send nab to its ranged-read rung, which would
                     bypass pip's downloader, its cache and its hashes
``upload_time``      ``None``. pip already applied ``--uploaded-prior-to``
``hashes``           ``()``. pip verifies its own downloads
``local_path``       ``None``, always. A path here sends nab to its own
                     wheel reader, around pip's preparer
``metadata_hash``    ``None``. See ``hashes``
===================  =========================================================
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pip._vendor.nab_index.client import SdistFile, WheelFile
from pip._vendor.nab_python.provider import MetadataError
from pip._vendor.nab_python.store import InMemoryIndex
from pip._vendor.packaging.version import Version

from pip._internal.resolution.nab.candidates import CandidateUnavailable

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from pip._vendor.packaging.specifiers import SpecifierSet
    from pip._vendor.packaging.utils import NormalizedName

    from pip._internal.metadata import BaseDistribution
    from pip._internal.resolution.nab.candidates import HostCandidate, PipHostIndex

logger = logging.getLogger(__name__)

# The index name nab records against a listing. pip resolves one merged
# index universe, so there is exactly one.
_SERVING_INDEX = "pip"


class LazyFetch:
    """A ``Waitable`` whose work runs on ``wait()``.

    ``threading.Event`` is what nab's coordinator returns and what nab's
    port declares; the provider only ever calls ``.wait()`` on it. That is
    the whole reason a synchronous host can drive nab's provider: a
    fire-and-forget prefetch becomes free and a blocking read pays for
    exactly itself.
    """

    __slots__ = ("_pending", "_work")

    def __init__(self, work: Callable[[], None] | None) -> None:
        self._work = work
        self._pending = work is not None

    def wait(self, timeout: float | None = None) -> bool:
        if self._pending:
            self._pending = False
            assert self._work is not None
            self._work()
        return True


_SETTLED = LazyFetch(None)


class PipFetchPort:
    """``nab_python.fetch_port.FetchPort`` over pip's index layer.

    Five of the nine members are live. ``request_range_metadata``,
    ``request_sdist_archive`` and ``request_direct_archive`` raise: pip owns
    ranged reads (``--use-feature=fast-deps``), sdist builds and direct URLs
    above this port, so a call to any of them means nab reached a rung this
    seam does not route, which is a defect and not a fallback.
    """

    def __init__(
        self,
        *,
        host: PipHostIndex,
        python_version: Version,
        ignore_requires_python: bool,
        ignore_dependencies: bool = False,
    ) -> None:
        self._host = host
        self._python_version = python_version
        self._ignore_requires_python = ignore_requires_python
        self._ignore_dependencies = ignore_dependencies
        self.index = InMemoryIndex()
        self._listed: set[str] = set()
        self._fetched: set[tuple[str, str]] = set()
        # Requires-Python read off a prepared distribution rather than off the
        # index page, per package, so a package emptied that way can still say
        # so. pip's own resolver reaches the same fact through the synthetic
        # Python package a candidate depends on.
        self.requires_python_refused: dict[
            NormalizedName, dict[Version, SpecifierSet]
        ] = {}

    # ------------------------------------------------------- the port

    @property
    def offline(self) -> bool:
        """pip has no offline resolve mode."""
        return False

    def request_listing(self, package: str) -> LazyFetch:
        if package in self._listed:
            return _SETTLED
        return LazyFetch(lambda: self._fill_listing(package))

    def request_metadata(
        self,
        package: str,
        version: str,
        url: str,
        metadata_hash: tuple[str, str] | None = None,
    ) -> LazyFetch:
        if (package, url) in self._fetched:
            return _SETTLED
        return LazyFetch(lambda: self._fill_metadata(package, version, url, url))

    def request_metadata_batch(
        self, items: Sequence[tuple[str, str, str, tuple[str, str] | None]]
    ) -> list[tuple[str, str, LazyFetch]]:
        """One waitable per item, none of them started.

        pip has no concurrency to batch onto: ``Downloader.batch`` is a for
        loop. Under nab's coordinator a batch is one submission of many
        fetches; here it is many recordings of one fetch each, and the
        caller decides which of them it actually reads.
        """
        return [
            (package, version, self.request_metadata(package, version, url, hashes))
            for package, version, url, hashes in items
        ]

    def request_sdist(
        self,
        package: str,
        version: str,
        url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> LazyFetch:
        """Rung 5, which pip answers with a prepared distribution.

        nab reads an sdist's ``PKG-INFO`` and puts it through PEP 643's
        dynamic-metadata gate; pip builds the sdist and the result is
        authoritative. So the text is stored as the version's metadata
        rather than as sdist-origin text, which is the store's own way of
        saying "this is not a PKG-INFO guess".
        """
        if (package, url) in self._fetched:
            return _SETTLED
        return LazyFetch(lambda: self._fill_metadata(package, version, url, None))

    def request_range_metadata(
        self,
        package: str,
        version: str,
        wheel_url: str,
        wheel_hashes: tuple[tuple[str, str], ...] = (),
    ) -> LazyFetch:
        raise NotImplementedError(
            f"nab asked to recover {package} {version} metadata with ranged "
            "reads, but every wheel this port publishes carries a sidecar"
        )

    def request_sdist_archive(
        self,
        package: str,
        version: str,
        url: str,
        sdist_hashes: tuple[tuple[str, str], ...] = (),
    ) -> LazyFetch:
        raise NotImplementedError(
            f"nab asked to download {package} {version} for a remote build; "
            "pip builds above this port"
        )

    def request_direct_archive(self, package: str, version: str, url: str) -> LazyFetch:
        raise NotImplementedError(
            f"nab asked to download {package} {version} from {url}; pip owns "
            "direct URLs above this port"
        )

    # ---------------------------------------------------------- work

    def _fill_listing(self, package: str) -> None:
        """Publish pip's candidate universe for ``package`` as nab records.

        Any ``InstallationError`` out of the finder propagates rather than
        landing in the listing's error slot: ``--uploaded-prior-to`` against
        an index that publishes no upload times is an abort, and recording
        it as a listing error would turn it into "no such package".
        """
        self._listed.add(package)
        if self.index.get_listing(package) is not None:
            return
        files = [_dist_file(candidate) for candidate in self._host.candidates(package)]
        self.index.store_listing(package, files)
        self.index.store_listing_index(package, _SERVING_INDEX)

    def _fill_metadata(
        self, package: str, version: str, key_url: str, slot_url: str | None
    ) -> None:
        """Prepare one candidate and publish its METADATA.

        ``key_url`` is the artifact this request dedupes on and ``slot_url``
        is where the store keeps the answer: a wheel's sidecar answers for
        that sidecar, while an sdist's build answers for the whole version,
        because nab reads the sdist rung out of the version-level slot.

        A candidate pip cannot prepare is recorded as a metadata error, which
        nab's look-ahead reads as a rejection and skips, exactly as it treats
        a version whose own metadata will not parse.
        """
        self._fetched.add((package, key_url))
        candidate = self._host.find(package, Version(version))
        if candidate is None:
            self.index.store_metadata(package, version, None, slot_url)
            return
        try:
            dist = self._host.metadata(candidate)
        except CandidateUnavailable as exc:
            self._refuse(package, version, slot_url, exc.reason)
            return
        refusal = self._requires_python_refusal(dist)
        if refusal is not None:
            self.requires_python_refused.setdefault(package, {})[
                candidate.version
            ] = refusal
            self._refuse(
                package,
                version,
                slot_url,
                f"requires a different Python: {self._python_version} not in "
                f"{str(refusal)!r}",
            )
            return
        requires = self._host.adopt_dependencies(candidate, dist)
        if self._ignore_dependencies:
            # ``--no-deps``. nab's provider has no switch for it, because the
            # dependency expansion is also how an extras proxy learns which
            # versions provide its extra, so the requirements are dropped at
            # the source instead. pip does the same thing one level up, in
            # ``Candidate.iter_dependencies(with_requires=False)``.
            requires = []
        self.index.store_metadata(
            package, version, _metadata_text(dist, requires), slot_url
        )

    def _refuse(
        self, package: str, version: str, slot_url: str | None, reason: str
    ) -> None:
        logger.debug("skipping %s %s: %s", package, version, reason)
        self.index.store_metadata_error(
            package, version, MetadataError(reason), slot_url
        )

    def _requires_python_refusal(self, dist: BaseDistribution) -> SpecifierSet | None:
        """The distribution's own Requires-Python, when it excludes the target.

        pip's ``LinkEvaluator`` applies the index page's ``requires-python``
        hint, and a wheel whose METADATA declares one the page omitted is
        caught downstream, by the synthetic Python package every pip
        candidate depends on. nab applies the same rule off a
        ``ResolveTarget``, and this seam passes none, so pip keeps its own.
        """
        if self._ignore_requires_python:
            return None
        requires_python = dist.requires_python
        if not requires_python:
            return None
        if requires_python.contains(self._python_version, prereleases=True):
            return None
        return requires_python


def _dist_file(candidate: HostCandidate) -> WheelFile | SdistFile:
    """One nab record for one pip candidate.

    An installed distribution has no artifact, so it is published as an
    sdist record with a URL nothing fetches: nab needs a ``DistFile`` to
    carry the version, and the metadata for it arrives through the sdist
    rung, which this port answers out of the installed dist itself.
    """
    version = str(candidate.version)
    link = candidate.link
    if link is None:
        return SdistFile(
            filename=f"{candidate.project_name}-{version}.tar.gz",
            url=f"nab-installed:///{candidate.project_name}/{version}",
            version=version,
            requires_python=None,
            upload_time=None,
            hashes=(),
        )
    if link.is_wheel:
        return WheelFile(
            filename=link.filename,
            url=link.url,
            version=version,
            requires_python=None,
            has_metadata=True,
            upload_time=None,
            hashes=(),
            metadata_hash=None,
        )
    return SdistFile(
        filename=link.filename,
        url=link.url,
        version=version,
        requires_python=None,
        upload_time=None,
        hashes=(),
    )


def _metadata_text(dist: BaseDistribution, requires: Sequence[str]) -> str:
    """The METADATA nab parses, rebuilt from what pip prepared.

    Only the six fields nab's parser reads are emitted. Re-serialising
    ``dist.metadata`` would carry description bodies and RFC 822 folding
    that nothing downstream looks at, and this way the text cannot depend
    on how the artifact happened to be laid out.
    """
    lines = [
        "Metadata-Version: 2.1",
        f"Name: {dist.raw_name}",
        f"Version: {dist.version}",
    ]
    requires_python = dist.requires_python
    if requires_python:
        lines.append(f"Requires-Python: {requires_python}")
    lines.extend(f"Provides-Extra: {extra}" for extra in dist.iter_provided_extras())
    lines.extend(f"Requires-Dist: {line}" for line in requires)
    return "\n".join(lines) + "\n\n"
