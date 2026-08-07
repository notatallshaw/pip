"""The candidate universe pip hands the nab resolver.

Under this arm pip owns the whole index layer and nab owns only the search,
so the adapter supplies a universe of versions rather than letting nab list
anything. Three properties have to hold and each one is load-bearing:

1. It contains every version that could be selected. nab's widening records
   one set of dependencies for a whole range of versions, and that is only
   sound when no selectable version inside the range is missing from the
   universe. pip's ``find_all_candidates`` qualifies because
   ``RequirementCommand._build_package_finder`` sets ``allow_yanked=True``
   unconditionally, so ``LinkEvaluator`` never removes a yanked file and
   everything else it removes is genuinely unselectable.

2. It is ordered by version. ``CandidateEvaluator._sort_key`` leads with
   ``has_allowed_hash`` and ``yank_value``, so pip's own ranking is not
   version-monotonic and cannot be used as an ordering over versions. The
   ranking is still what decides which *file* represents a version, so the
   universe is built by ranking first and then grouping by version.

   Sorting by version keeps property 1 and drops one thing pip's ranking
   was carrying: ``binary_preference``, the ``--prefer-binary`` element,
   which sits *above* the version in ``_sort_key`` and so orders a 0.8
   wheel ahead of a 1.0 sdist. That is a preference between versions, not
   between the files of one version, so it cannot live in this ordering.
   Each record carries :attr:`HostCandidate.is_binary` instead and the
   engine applies it where it decides which version to try first.

3. Yanked versions are carried, flagged, never dropped here. PEP 592 makes a
   yanked version selectable exactly when the requirement pins it, and
   whether it is pinned is not known until the requirements have been merged.
   Dropping a yanked version at supply time turns ``pip install foo==1.0``
   into a resolution failure when 1.0 is yanked, which pip resolves today.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import InvalidVersion

from pip._internal.exceptions import (
    InvalidInstalledPackage,
    MetadataInconsistent,
    MetadataInvalid,
    UnsupportedWheel,
)
from pip._internal.models.link import links_equivalent
from pip._internal.req.constructors import (
    install_req_drop_extras,
    install_req_from_link_and_ireq,
)
from pip._internal.utils.hashes import Hashes

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pip._vendor.packaging.specifiers import SpecifierSet
    from pip._vendor.packaging.utils import NormalizedName
    from pip._vendor.packaging.version import Version

    from pip._internal.index.package_finder import PackageFinder
    from pip._internal.metadata import BaseDistribution
    from pip._internal.models.candidate import InstallationCandidate
    from pip._internal.models.link import Link
    from pip._internal.req.req_install import InstallRequirement
    from pip._internal.resolution.base import InstallRequirementProvider
    from pip._internal.resolution.model.base import Candidate
    from pip._internal.resolution.model.factory import Factory
    from pip._internal.resolution.nab.inputs import ResolveInputs

logger = logging.getLogger(__name__)


class CandidateUnavailable(Exception):
    """This version exists but its metadata cannot be used.

    Raised by :meth:`PipHostIndex.metadata`. The engine seam turns it into
    the rejection nab already understands, which drops the version and keeps
    searching rather than failing the resolve.
    """

    def __init__(self, project_name: str, version: Version, reason: str) -> None:
        super().__init__(f"{project_name} {version}: {reason}")
        self.project_name = project_name
        self.version = version
        self.reason = reason


@dataclass(frozen=True)
class HostCandidate:
    """One selectable version of one project, as pip sees it.

    Exactly one of the three sources is set. An installed distribution has
    no artifact, so it can never route through a download, and an explicit
    link replaces the listing rather than joining it.

    ``is_binary`` is what ``--prefer-binary`` sorts on: True when this
    version needs no build, which is a wheel or a distribution that is
    already installed. It is recorded unconditionally and read only when
    the user asked for the preference, so the flag decides whether it is
    used and never what it says.
    """

    project_name: NormalizedName
    version: Version
    yanked: bool
    is_binary: bool = False
    index_candidate: InstallationCandidate | None = None
    installed_dist: BaseDistribution | None = None
    explicit_link: Link | None = None

    @property
    def is_installed(self) -> bool:
        return self.installed_dist is not None


@dataclass(frozen=True)
class CandidateMetadata:
    """The metadata a resolver needs about one version.

    ``raw_dependencies`` are unevaluated PEP 508 strings, including the
    ``; extra == "x"`` lines. ``BaseDistribution.iter_dependencies`` cannot be
    used to produce them: it evaluates every marker against the live
    environment with ``extra`` bound to the empty string, which drops every
    extra-gated line and pre-consumes every environment marker.
    """

    project_name: NormalizedName
    version: Version
    requires_python: SpecifierSet | None
    raw_dependencies: tuple[str, ...]
    provided_extras: frozenset[NormalizedName]


class PipHostIndex:
    """Answers "which versions exist" and "what does this version need".

    Both answers come from pip's own machinery, so format control, wheel tag
    compatibility, Requires-Python, ``--platform``, ``--prefer-binary``,
    ``--uploaded-prior-to``, hash intersection, the built wheel cache,
    build isolation, direct URLs, VCS and ``--find-links`` all keep working
    without the adapter knowing they exist.
    """

    def __init__(
        self,
        *,
        factory: Factory,
        finder: PackageFinder,
        inputs: ResolveInputs,
        upgrade_strategy: str,
        make_install_req: InstallRequirementProvider,
    ) -> None:
        self._factory = factory
        self._finder = finder
        self._inputs = inputs
        self._upgrade_strategy = upgrade_strategy
        self._make_install_req = make_install_req
        self._universe: dict[NormalizedName, Sequence[HostCandidate]] = {}
        self._installed_records: dict[NormalizedName, HostCandidate | None] = {}
        self._templates: dict[NormalizedName, InstallRequirement] = {}
        self._index_templates: dict[NormalizedName, InstallRequirement] = {}
        self._comes_from: dict[NormalizedName, InstallRequirement] = {}
        self._requested_as: dict[NormalizedName, str] = {}
        self._pip_candidates: dict[
            tuple[NormalizedName, Version, frozenset[NormalizedName]], Candidate
        ] = {}

    def candidates(self, project_name: NormalizedName) -> Sequence[HostCandidate]:
        """Every selectable version of ``project_name``, oldest first.

        One entry per version. Any ``InstallationError`` raised out of the
        finder propagates: ``--uploaded-prior-to`` against an index that
        publishes no upload times is an abort, not an empty result, and
        swallowing it here would silently turn it into "no such package".
        """
        cached = self._universe.get(project_name)
        if cached is not None:
            return cached
        universe = self._build_universe(project_name)
        self._universe[project_name] = universe
        return universe

    def is_listed(self, project_name: NormalizedName) -> bool:
        """Has ``project_name``'s universe already been paid for?

        A caller that only wants to order its work can ask this first and
        answer from something else when it is False, rather than buying an
        index request to produce a number. pip's own resolver never lists a
        package to decide which package to look at next.
        """
        return project_name in self._universe

    def installed(self, project_name: NormalizedName) -> HostCandidate | None:
        """The installed distribution, without listing the index.

        This is the one fact about a package pip is already holding, and it
        is the first element of the sequence pip's own resolver searches
        (``found_candidates._iter_built_with_prepended``). Returning it here
        is what lets a requirement something already satisfies be answered
        with no index request at all.

        None when nothing is installed, when the index has to answer instead
        (``--force-reinstall``, ``--ignore-installed``), or when the package
        is pinned to a URL, path, VCS ref or editable, because then the
        universe is that link and what is on disk is not in it.

        The explicit test is repeated on every call rather than memoised
        with the record: ``register_explicit`` can give a package an explicit
        universe part way through a resolve, and a remembered "no explicit
        source" would then keep handing out a candidate that is no longer in
        the universe.
        """
        if self._has_explicit_universe(project_name):
            return None
        if project_name in self._installed_records:
            return self._installed_records[project_name]
        record = self._installed_candidate(project_name)
        self._installed_records[project_name] = record
        return record

    def find(
        self, project_name: NormalizedName, version: Version
    ) -> HostCandidate | None:
        """The record for one version, listing only when it has to.

        The installed distribution answers for itself, which keeps a resolve
        that never listed a package from listing it on the way out.
        ``_build_universe`` puts the very same record in the universe in
        place of the index entry for that version, so the two paths cannot
        disagree.
        """
        installed = self.installed(project_name)
        if installed is not None and installed.version == version:
            return installed
        for candidate in self.candidates(project_name):
            if candidate.version == version:
                return candidate
        return None

    def preferred_version(self, project_name: NormalizedName) -> Version | None:
        """The installed version, when this package must not be upgraded.

        pip expresses "prefer what is installed" by ordering candidates
        (``_iter_built_with_prepended``). Under this arm the universe is
        ordered by version, so the preference is passed to the engine
        separately instead.
        """
        if self._eligible_for_upgrade(project_name):
            return None
        dist = self._installed_dist(project_name)
        if dist is None:
            return None
        return self._installed_version(dist)

    def metadata(self, candidate: HostCandidate) -> CandidateMetadata:
        """Prepare ``candidate`` and read its metadata.

        This is where pip downloads, builds and validates, so a candidate
        probe costs what a candidate probe has always cost pip.
        """
        pip_candidate = self.pip_candidate(candidate, frozenset())
        dist = pip_candidate.dist  # type: ignore[attr-defined]
        return CandidateMetadata(
            project_name=canonicalize_name(dist.raw_name),
            version=dist.version,
            requires_python=dist.requires_python or None,
            raw_dependencies=tuple(dist.iter_raw_dependencies()),
            provided_extras=frozenset(dist.iter_provided_extras()),
        )

    def pip_candidate(
        self, candidate: HostCandidate, extras: frozenset[NormalizedName]
    ) -> Candidate:
        """The pip ``Candidate`` for a chosen version, built at most once.

        The result adapter needs this object: it carries
        ``get_install_requirement()``, ``source_link``, ``is_editable`` and
        ``download_info``, which is how ``--report`` and ``pip lock`` are
        answered without a second fetch.
        """
        key = (candidate.project_name, candidate.version, extras)
        cached = self._pip_candidates.get(key)
        if cached is not None:
            return cached

        if candidate.explicit_link is None:
            template = self._index_template_for(candidate.project_name)
        else:
            template = self._template_for(candidate.project_name)
        built: Candidate | None
        try:
            if candidate.installed_dist is not None:
                built = self._factory._make_candidate_from_dist(
                    dist=candidate.installed_dist,
                    extras=frozenset(extras),
                    template=template,
                )
            else:
                link = (
                    candidate.explicit_link
                    if candidate.explicit_link is not None
                    else (
                        candidate.index_candidate.link
                        if candidate.index_candidate is not None
                        else None
                    )
                )
                assert link is not None, "a candidate must carry a source"
                built = self._factory._make_candidate_from_link(
                    link=link,
                    extras=frozenset(extras),
                    template=template,
                    name=candidate.project_name,
                    version=(
                        candidate.version if candidate.explicit_link is None else None
                    ),
                )
        except (MetadataInconsistent, MetadataInvalid) as exc:
            raise CandidateUnavailable(
                candidate.project_name, candidate.version, str(exc)
            ) from exc
        if built is None:
            raise CandidateUnavailable(
                candidate.project_name,
                candidate.version,
                "the distribution could not be prepared",
            )
        self._pip_candidates[key] = built
        return built

    def register_explicit(
        self, spec: str, comes_from: InstallRequirement | None
    ) -> bool:
        """Make a direct URL dependency the whole universe for its package.

        pip already behaves this way for a URL named on the command line:
        once any explicit candidate exists, ``Factory.find_candidates`` skips
        the finder. A URL that arrives as somebody's dependency is the same
        thing discovered later, so it is registered the same way.

        Returns False when the package's universe has already been handed to
        the engine from the index, because replacing it under the search
        would make an earlier answer unexplainable. That is a hard failure
        for the caller to report, not something to paper over.
        """
        ireq = self._make_install_req(spec, comes_from)
        assert ireq.name is not None, "a dependency always carries a name"
        project_name = canonicalize_name(ireq.name)
        existing = self._inputs.explicit.get(project_name)
        if existing:
            # Two spellings of one URL are one requirement: pip compares them
            # with links_equivalent, which ignores the ``#egg=`` fragment and
            # query-parameter order.
            assert ireq.link is not None
            return all(
                entry.link is not None and links_equivalent(entry.link, ireq.link)
                for entry in existing
            )
        if project_name in self._universe:
            return False
        self._inputs.explicit[project_name] = [ireq]
        self._templates[project_name] = ireq
        return True

    def note_requested_by(
        self,
        project_name: NormalizedName,
        raw_name: str,
        comes_from: InstallRequirement | None,
    ) -> None:
        """Record who first asked for ``project_name``, and how they spelled it.

        pip annotates a candidate with the requirement it came from, which is
        what puts ``(from pkg[ext])`` in the download line and in an error.
        A package reached only transitively has no root ireq to carry that,
        so the first parent to ask for it supplies one.

        The spelling matters too: pip builds the template from the
        requirement as written, so ``Installing collected packages`` says
        ``PySocks`` and not ``pysocks``. Synthesizing from the canonical name
        would quietly rename every transitively reached package.
        """
        if project_name in self._templates:
            return
        self._requested_as.setdefault(project_name, raw_name)
        if comes_from is not None:
            self._comes_from.setdefault(project_name, comes_from)

    def allows_prereleases(self, project_name: NormalizedName) -> bool | None:
        """``--pre`` and friends, for one project.

        True means the user asked for prereleases, False means only final
        versions, and None means "decide from the requirement", which is PEP
        440's rule and which only the resolver can apply because only it
        knows the merged range.
        """
        release_control = self._finder.release_control
        if release_control is None:
            return None
        return release_control.allows_prereleases(project_name)

    def prefers_binary(self) -> bool:
        """``--prefer-binary``: try every wheel before any source archive.

        Read live rather than at universe-build time, because a
        requirements file can turn it on after the finder was made
        (``req_file.py`` calls ``PackageFinder.set_prefer_binary``).
        """
        return self._finder.prefer_binary

    def hashes_for(self, project_name: NormalizedName) -> Hashes:
        """The hash allowlist the command line puts on ``project_name``."""
        hashes = Hashes()
        for requirement in self._inputs.requirements:
            if requirement.project_name == project_name:
                hashes &= requirement.ireq.hashes(trust_internet=False)
        constraint = self._inputs.constraints.get(project_name)
        if constraint is not None:
            hashes &= constraint.hashes
        return hashes

    def _build_universe(self, project_name: NormalizedName) -> Sequence[HostCandidate]:
        explicit = self._explicit_universe(project_name)
        if explicit is not None:
            return explicit

        evaluator = self._finder.make_candidate_evaluator(
            project_name=project_name,
            hashes=self.hashes_for(project_name),
        )
        ranked = evaluator.rank_candidates(
            self._finder.find_all_candidates(project_name)
        )

        # ``ranked`` is ascending by pip's preference, so the last entry for a
        # version is the file pip would pick for that version. Grouping this
        # way keeps pip's file choice while making the result version-ordered.
        best_by_version: dict[Version, InstallationCandidate] = {}
        for index_candidate in ranked:
            best_by_version[index_candidate.version] = index_candidate

        universe = [
            HostCandidate(
                project_name=project_name,
                version=version,
                yanked=index_candidate.link.is_yanked,
                is_binary=index_candidate.link.is_wheel,
                index_candidate=index_candidate,
            )
            for version, index_candidate in best_by_version.items()
        ]

        installed = self.installed(project_name)
        if installed is not None:
            # An installed version replaces the index entry for the same
            # version: pip's merge iterators yield the installed candidate
            # instead of building the index one.
            universe = [
                entry for entry in universe if entry.version != installed.version
            ]
            universe.append(installed)

        universe.sort(key=lambda entry: entry.version)
        return tuple(universe)

    def _has_explicit_universe(self, project_name: NormalizedName) -> bool:
        """Is this package's universe a link rather than the index?

        The same two sources ``_explicit_universe`` reads, tested without
        building anything: a requirement naming a link, or a constraint
        whose lines name one.
        """
        if project_name in self._inputs.explicit:
            return True
        constraint = self._inputs.constraints.get(project_name)
        return constraint is not None and bool(constraint.links)

    def _explicit_universe(
        self, project_name: NormalizedName
    ) -> Sequence[HostCandidate] | None:
        """The universe for a package pinned to a URL, path, VCS ref or editable.

        pip already behaves this way: the moment any explicit candidate
        exists, ``Factory.find_candidates`` skips the finder entirely and
        returns only the explicit set. Returns None when the package has no
        explicit source and the finder should answer instead.

        Two sources of explicit candidates, and pip does not merge them. A
        requirement naming a link wins outright, and the URL constraints then
        act as a filter over it. Only when no requirement names a link do the
        URL constraints themselves become the candidates.
        """
        constraint = self._inputs.constraints.get(project_name)
        ireqs = self._inputs.explicit.get(project_name)

        if ireqs:
            if self._distinct_links(ireqs) > 1:
                # An explicit candidate has to satisfy every explicit
                # requirement, and two different URLs never both do, so pip's
                # own ``find_candidates`` leaves this package with nothing.
                return ()
            ireq = ireqs[0]
            assert ireq.link is not None
            self._templates.setdefault(project_name, ireq)
            candidate = self._build_explicit(project_name, ireq.link, ireq)
            if candidate is None:
                # An unnamed URL fails eagerly while it is being named; a
                # named one becomes unsatisfiable so the search can back out.
                return ()
            if constraint is not None and not constraint.is_satisfied_by(candidate):
                return ()
            return (self._explicit_record(project_name, candidate, ireq.link),)

        if constraint is None or not constraint.links:
            return None

        template = self._template_for(project_name)
        records: list[HostCandidate] = []
        for link in constraint.links:
            candidate = self._build_explicit(
                project_name, link, install_req_from_link_and_ireq(link, template)
            )
            if candidate is None:
                continue
            # Every URL constraint has to match, not one of them: two
            # constraint lines naming different URLs for one package leave it
            # with nothing, which is what pip reports.
            if not constraint.is_satisfied_by(candidate):
                continue
            records.append(self._explicit_record(project_name, candidate, link))
        records.sort(key=lambda record: record.version)
        return tuple(records)

    @staticmethod
    def _distinct_links(ireqs: Sequence[InstallRequirement]) -> int:
        distinct: list[Link] = []
        for ireq in ireqs:
            assert ireq.link is not None
            if not any(links_equivalent(link, ireq.link) for link in distinct):
                distinct.append(ireq.link)
        return len(distinct)

    def explicit_candidate(
        self,
        project_name: NormalizedName,
        ireq: InstallRequirement,
        extras: frozenset[NormalizedName],
    ) -> Candidate | None:
        """The candidate one link-backed root builds, for the error message.

        Deliberately not read from the universe. The message has to name the
        distribution in exactly the two cases the universe is empty: a
        constraint excluded it, or a second URL contradicted it. The template
        drops the extras the way ``_make_requirements_from_install_req``
        does, because the base candidate is what the extras ride on.
        """
        assert ireq.link is not None
        template = install_req_drop_extras(ireq) if ireq.extras else ireq
        base = self._build_explicit(project_name, ireq.link, template)
        if base is None or not extras:
            return base
        return self._factory._make_extras_candidate(base, frozenset(extras))

    def _build_explicit(
        self, project_name: NormalizedName, link: Link, template: InstallRequirement
    ) -> Candidate | None:
        try:
            self._factory._fail_if_link_is_unsupported_wheel(link)
        except UnsupportedWheel:
            # Constrained to a wheel this platform cannot use, so no candidate
            # will ever be valid.
            return None
        return self._factory._make_base_candidate_from_link(
            link,
            template=template,
            name=project_name,
            version=None,
        )

    @staticmethod
    def _explicit_record(
        project_name: NormalizedName, candidate: Candidate, link: Link
    ) -> HostCandidate:
        return HostCandidate(
            project_name=project_name,
            version=candidate.version,
            yanked=link.is_yanked,
            is_binary=link.is_wheel,
            explicit_link=link,
        )

    def _installed_candidate(
        self, project_name: NormalizedName
    ) -> HostCandidate | None:
        dist = self._installed_dist(project_name)
        if dist is None:
            return None
        return HostCandidate(
            project_name=project_name,
            version=self._installed_version(dist),
            yanked=False,
            # Nothing to download and nothing to build, so it is what
            # --prefer-binary asks for. pip never runs _sort_key over an
            # installed candidate, and the "prefer what is installed" rule
            # is applied after this one, so the flag can only move an
            # installed version that pip was already free to upgrade past.
            is_binary=True,
            installed_dist=dist,
        )

    def _installed_dist(self, project_name: NormalizedName) -> BaseDistribution | None:
        # --force-reinstall means take the index version, so pretend nothing
        # is installed.
        if self._factory.force_reinstall:
            return None
        return self._factory._installed_dists.get(project_name)

    @staticmethod
    def _installed_version(dist: BaseDistribution) -> Version:
        try:
            return dist.version
        except InvalidVersion as exc:
            raise InvalidInstalledPackage(dist=dist, invalid_exc=exc) from exc

    def _eligible_for_upgrade(self, project_name: NormalizedName) -> bool:
        """Are upgrades allowed for this project?

        Same rule as ``PipProvider.find_matches``: eager upgrades everything,
        only-if-needed upgrades what the user named, to-satisfy-only upgrades
        nothing.
        """
        if self._upgrade_strategy == "eager":
            return True
        if self._upgrade_strategy == "only-if-needed":
            return any(
                canonicalize_name(key.partition("[")[0]) == project_name
                for key in self._inputs.user_requested
            )
        return False

    def _template_for(self, project_name: NormalizedName) -> InstallRequirement:
        """The ireq pip's candidate machinery uses as a template.

        pip takes the first requirement that mentions the project. A package
        reached only transitively has no root ireq, so one is synthesized the
        way ``Factory.make_requirements_from_spec`` does.
        """
        cached = self._templates.get(project_name)
        if cached is not None:
            return cached
        for requirement in self._inputs.requirements:
            if requirement.project_name == project_name:
                self._templates[project_name] = requirement.ireq
                return requirement.ireq
        template = self._make_install_req(
            self._requested_as.get(project_name, project_name),
            self._comes_from.get(project_name),
        )
        self._templates[project_name] = template
        return template

    def _index_template_for(self, project_name: NormalizedName) -> InstallRequirement:
        """The template for a candidate that came out of the index.

        A constraint may carry the ``--hash`` lines for a requirement that
        carries none, and pip lets it: ``_iter_found_candidates`` copies the
        constraint's hash options onto the template, and
        ``make_install_req_from_link`` then writes the candidate's install
        requirement as ``name==version`` instead of repeating the unpinned
        requirement. That is what keeps ``--require-hashes`` from rejecting
        it as ``HashUnpinned``, which is pypa/pip#9243.

        The copy is what pip does too. The requirement it copies is a root
        ireq shared with the requirement set, so setting the hash options on
        it directly would change the user's own requirement.
        """
        cached = self._index_templates.get(project_name)
        if cached is not None:
            return cached
        template = self._template_for(project_name)
        constraint = self._inputs.constraints.get(project_name)
        if (
            constraint is not None
            and not template.hash_options
            and any(constraint.hash_options.values())
        ):
            template = copy.copy(template)
            template.hash_options = {
                algorithm: list(digests)
                for algorithm, digests in constraint.hash_options.items()
            }
        self._index_templates[project_name] = template
        return template

    def preferences(self) -> Mapping[str, Version]:
        """Versions the engine should try first, keyed by project name."""
        preferences: dict[str, Version] = {}
        for project_name in self._universe:
            version = self.preferred_version(project_name)
            if version is not None:
                preferences[project_name] = version
        return preferences
