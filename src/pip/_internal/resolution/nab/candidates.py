"""The candidate universe and the metadata pip hands nab's provider.

pip owns the index layer: which files exist, which of them this machine can
install, which one represents a version, and what preparing one costs.
nab's provider owns everything above that: the ladder that decides where
metadata comes from, the priority key, the widening, the yank rule and the
prerelease admission. This module is the pip half, and
:mod:`.fetch_port` is the adapter that publishes it in nab's shapes.

Three properties have to hold of the universe and each one is load-bearing:

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

   Sorting by version drops the one term of ``_sort_key`` that is a
   preference *between* versions rather than between the files of one
   version: ``binary_preference``, which is ``--prefer-binary`` and which
   sits above the version, so a 0.8 wheel is preferred to a 1.0 source
   archive. Each record carries :attr:`HostCandidate.is_binary` instead,
   and :mod:`.engine` applies the preference where the versions still in
   range are known, which is where pip applies it too.

3. Yanked versions are carried, flagged, never dropped here. PEP 592 makes a
   yanked version selectable exactly when the requirement pins it, and
   whether it is pinned is not known until the requirements have been merged.
   Dropping a yanked version at supply time turns ``pip install foo==1.0``
   into a resolution failure when 1.0 is yanked, which pip resolves today.
   nab applies the rule, through the ``YankPolicy`` this module's facts feed.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pip._vendor.packaging.requirements import InvalidRequirement
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import InvalidVersion

from pip._internal.exceptions import (
    InstallationError,
    InvalidInstalledPackage,
    MetadataInconsistent,
    MetadataInvalid,
    UnsupportedWheel,
)
from pip._internal.models.link import links_equivalent
from pip._internal.req.constructors import (
    install_req_drop_extras,
    install_req_from_line,
    install_req_from_link_and_ireq,
)
from pip._internal.utils.hashes import Hashes
from pip._internal.utils.packaging import get_requirement

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pip._vendor.packaging.requirements import Requirement
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
    from pip._internal.resolution.nab.inputs import ResolveInputs, RootRequirement

logger = logging.getLogger(__name__)


class CandidateUnavailable(Exception):
    """This version exists but its metadata cannot be used.

    Raised by :meth:`PipHostIndex.metadata`. The fetch port records it as a
    metadata error, which is the rejection nab already understands: the
    version is dropped and the search keeps going.
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
    already installed. It is recorded whether or not the user asked for the
    preference, so the flag decides whether it is read and never what it
    says.
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

    @property
    def link(self) -> Link | None:
        """The artifact this version is served from, or None if installed."""
        if self.explicit_link is not None:
            return self.explicit_link
        if self.index_candidate is not None:
            return self.index_candidate.link
        return None


class PipHostIndex:
    """Answers "which versions exist" and "what does this version need".

    Both answers come from pip's own machinery, so format control, wheel tag
    compatibility, Requires-Python, ``--platform``, ``--prefer-binary``,
    ``--uploaded-prior-to``, hash intersection, the built wheel cache,
    build isolation, direct URLs, VCS and ``--find-links`` all keep working
    without nab knowing they exist.
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
        self._installed: dict[NormalizedName, HostCandidate | None] = {}
        self._binary_versions: dict[NormalizedName, frozenset[Version]] = {}
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

    def installed(self, project_name: NormalizedName) -> HostCandidate | None:
        """The installed distribution as a candidate, without listing.

        ``None`` when nothing is installed, when ``--force-reinstall`` or
        ``--ignore-installed`` means the index answers instead, or when the
        package has an explicit universe: a requirement or constraint naming
        a URL, path, VCS ref or editable replaces the index entirely
        (:meth:`_explicit_universe`), and what is installed is not in it.

        The explicit test is made on every call rather than memoised with
        the candidate, because :meth:`register_explicit` can give a package
        an explicit universe part way through a resolve.
        """
        if self._has_explicit_universe(project_name):
            return None
        if project_name not in self._installed:
            self._installed[project_name] = self._installed_candidate(project_name)
        return self._installed[project_name]

    def _has_explicit_universe(self, project_name: NormalizedName) -> bool:
        if project_name in self._inputs.explicit:
            return True
        constraint = self._inputs.constraints.get(project_name)
        return constraint is not None and bool(constraint.links)

    def find(
        self, project_name: NormalizedName, version: Version
    ) -> HostCandidate | None:
        """The candidate for one version, or None if nothing supplies it.

        The installed distribution answers for its own version without a
        listing. That is not a shortcut with a different answer: when the
        universe is built, an installed version replaces the index entry for
        the same version (see :meth:`_build_universe`), so this returns the
        object the full universe would have returned.
        """
        installed = self.installed(project_name)
        if installed is not None and installed.version == version:
            return installed
        for candidate in self.candidates(project_name):
            if candidate.version == version:
                return candidate
        return None

    def yanked_versions(self, project_name: NormalizedName) -> frozenset[Version]:
        """Which of ``project_name``'s versions the index marks yanked.

        One half of PEP 592. nab asks for it once per selection that has a
        yanked candidate in range, and applies the rule itself.
        """
        return frozenset(
            candidate.version
            for candidate in self.candidates(project_name)
            if candidate.yanked
        )

    def yanked_among(
        self, project_name: NormalizedName, candidates: Sequence[Version]
    ) -> frozenset[Version]:
        """Which of ``candidates`` the index marks yanked.

        An installed distribution has no index file and no yank flag, and it
        is the entry :meth:`_build_universe` keeps for its version, so a
        question only about it is answered without listing anything.
        """
        wanted = set(candidates)
        installed = self.installed(project_name)
        if installed is not None:
            wanted.discard(installed.version)
        if not wanted:
            return frozenset()
        return frozenset(self.yanked_versions(project_name) & wanted)

    def allows_prereleases(self, project_name: NormalizedName) -> bool:
        """``--pre`` and its friends, for one project.

        Only the admitting side. ``--only-final`` is applied to the universe
        by ``CandidateEvaluator.rank_candidates``, which is the one place pip
        applies it; re-applying it here would also drop a pre-release named
        by a URL or by an exact pin, which pip installs. Read live, because
        a requirements file can set release control after the finder was
        made.
        """
        release_control = self._finder.release_control
        if release_control is None:
            return False
        return release_control.allows_prereleases(project_name) is True

    def installed_versions(self, project_name: NormalizedName) -> frozenset[Version]:
        """The installed version, which pip admits on bounds alone.

        ``Factory._iter_found_candidates`` asks only whether what is already
        there still fits
        (``specifier.contains(installed_dist.version, prereleases=True)``),
        so an installed ``2.0rc1`` survives a plain ``pip install pkg`` and
        falls to a ``pkg<2``.

        Not the same set as the version :meth:`installed` seeds the provider
        with. The seed is skipped for a package the upgrade strategy allows
        pip to move; the admission applies whatever the strategy is, because
        pip's ``_iter_built_with_inserted`` still yields the installed
        candidate under ``--upgrade``, just in version order rather than
        first.
        """
        candidate = self.installed(project_name)
        if candidate is None:
            return frozenset()
        return frozenset((candidate.version,))

    def prefers_binary(self) -> bool:
        """``--prefer-binary``: try every wheel before any source archive.

        Read live rather than once at construction, because a requirements
        file can turn it on after the finder was built (``req_file.py``
        calls ``PackageFinder.set_prefer_binary``).
        """
        return self._finder.prefer_binary

    def binary_versions(self, project_name: NormalizedName) -> frozenset[Version]:
        """Which of ``project_name``'s versions need no build.

        The other half of ``--prefer-binary``. Answered off the universe
        rather than off the finder, so it names the file pip's ranking
        actually chose for each version.
        """
        cached = self._binary_versions.get(project_name)
        if cached is None:
            cached = frozenset(
                candidate.version
                for candidate in self.candidates(project_name)
                if candidate.is_binary
            )
            self._binary_versions[project_name] = cached
        return cached

    def metadata(self, candidate: HostCandidate) -> BaseDistribution:
        """Prepare ``candidate`` and return pip's distribution for it.

        This is where pip downloads, builds and validates, so a candidate
        probe costs what a candidate probe has always cost pip.
        """
        return self.pip_candidate(candidate, frozenset()).dist  # type: ignore[attr-defined]

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
                link = candidate.link
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
        except MetadataInvalid as exc:
            # pip warns about this one rather than dropping it quietly, and
            # this is the only place a version is dropped for it now that
            # ``FoundCandidates`` is off the resolve path.
            logger.warning(
                "Ignoring version %s of %s since it has invalid metadata:\n"
                "%s\n"
                "Please use pip<24.1 if you need to use this version.",
                candidate.version,
                exc.ireq.name,
                exc,
            )
            raise CandidateUnavailable(
                candidate.project_name, candidate.version, str(exc)
            ) from exc
        except MetadataInconsistent as exc:
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

    def adopt_dependencies(
        self, candidate: HostCandidate, dist: BaseDistribution
    ) -> list[str]:
        """Take over what only pip can own, and hand the rest back as written.

        Two things happen to every dependency line before nab sees it.

        The spelling and the parent are recorded. pip annotates a candidate
        with the requirement it came from, which is what puts ``(from pkg)``
        in the download line and in an error, and it builds the template from
        the requirement *as written*, so ``Installing collected packages``
        says ``PySocks`` and not ``pysocks``. nab hands the resolver
        canonical keys, so neither fact survives the seam and pip keeps them
        here, before nab has asked about the dependency.

        A direct URL is taken over entirely. nab refuses to resolve one and
        says so loudly; pip resolves it by making the link the package's
        whole universe, which is what ``Factory.find_candidates`` already
        does for a URL on the command line. So the URL is registered and the
        line handed on without it, and nab sees an ordinary dependency whose
        universe happens to hold exactly one version.
        """
        base = self.pip_candidate(candidate, frozenset()).get_install_requirement()
        provided = frozenset(dist.iter_provided_extras())
        parents: dict[frozenset[NormalizedName], InstallRequirement | None] = {}
        lines: list[str] = []
        for raw in dist.iter_raw_dependencies():
            line = raw.strip()
            if not line:
                continue
            try:
                requirement = get_requirement(line)
            except InvalidRequirement:
                # nab rejects the whole distribution for this, in its own
                # words. Passing the line through keeps that its decision.
                lines.append(line)
                continue
            extras = _activating_extras(requirement, provided)
            if extras not in parents:
                parents[extras] = self._extras_parent(base, extras)
            comes_from = parents[extras]
            name = canonicalize_name(requirement.name)
            if name not in self._templates:
                self._requested_as.setdefault(name, requirement.name)
                if comes_from is not None:
                    self._comes_from.setdefault(name, comes_from)
            if requirement.url is None:
                lines.append(line)
                continue
            if not self.register_explicit(line, comes_from):
                raise InstallationError(
                    f"Cannot install {line}: {name} was already resolved from "
                    "the index before this direct URL requirement was reached."
                )
            lines.append(_without_url(requirement))
        return lines

    @staticmethod
    def _extras_parent(
        base: InstallRequirement | None, extras: frozenset[NormalizedName]
    ) -> InstallRequirement | None:
        """The requirement a dependency behind ``extra == "x"`` comes from.

        pip credits it to the ``pkg[x]`` node and not to ``pkg``, and that is
        the name ``(from ...)`` carries. nab keys the two as separate
        packages, so the base candidate's ireq is the wrong one to hand on
        and the spelling with the extras is rebuilt from it.
        """
        if base is None or base.req is None or not extras:
            return base
        if extras <= {canonicalize_name(extra) for extra in base.req.extras}:
            return base
        return install_req_from_line(
            _with_extras(base.req, extras), comes_from=base.comes_from
        )

    def register_explicit(
        self, spec: str, comes_from: InstallRequirement | None
    ) -> bool:
        """Make a direct URL dependency the whole universe for its package.

        pip already behaves this way for a URL named on the command line:
        once any explicit candidate exists, ``Factory.find_candidates`` skips
        the finder. A URL that arrives as somebody's dependency is the same
        thing discovered later, so it is registered the same way, and nab
        then sees an ordinary package whose universe happens to hold one
        version.

        Returns False when the package's universe has already been handed
        over from the index, because replacing it under a live search would
        make an earlier answer unexplainable.
        """
        ireq = self._make_install_req(spec, comes_from)
        assert ireq.name is not None, "a dependency always carries a name"
        assert ireq.link is not None, "a direct URL requirement carries a link"
        project_name = canonicalize_name(ireq.name)
        existing = self._inputs.explicit.get(project_name, [])
        # Two spellings of one URL are one requirement: pip compares them
        # with links_equivalent, which ignores the ``#egg=`` fragment and
        # query-parameter order.
        for other in existing:
            assert other.link is not None
            if links_equivalent(other.link, ireq.link):
                return True
        if project_name in self._universe:
            return False
        if not existing:
            self._templates[project_name] = ireq
        self._inputs.explicit.setdefault(project_name, []).append(ireq)
        return True

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

        # :meth:`installed` rather than :meth:`_installed_candidate`, so the
        # universe carries the same record the seed path answered from. The
        # two agree here: this line is only reached when the package has no
        # explicit universe, which is the only thing the former adds.
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
        ireqs = self._inputs.explicit.get(project_name, [])

        if ireqs:
            if _distinct_links(ireqs) > 1:
                # An explicit candidate has to satisfy every requirement that
                # names a link, and two different URLs never both hold, so
                # the package is left with nothing. That is what pip
                # concludes too, and the error path names both requests.
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

    def explicit_candidate(
        self, root: RootRequirement, extras: frozenset[NormalizedName]
    ) -> Candidate | None:
        """The distribution a link-backed root names, constraint or not.

        :meth:`_explicit_universe` drops the candidate when a constraint
        excludes it or when a second URL contradicts it, which is exactly the
        failure pip has to name, so the error path asks here rather than
        through the universe. ``Factory._make_base_candidate_from_link``
        memoises on the link, so this is a cache hit for anything the search
        already built.
        """
        assert root.link is not None
        template = install_req_drop_extras(root.ireq) if root.ireq.extras else root.ireq
        base = self._factory._make_base_candidate_from_link(
            root.link, template=template, name=root.project_name, version=None
        )
        if base is None or not extras:
            return base
        return self._factory._make_extras_candidate(base, extras)

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
            # installed candidate, and "prefer what is installed" is applied
            # above this, so the flag can only move a version pip was
            # already free to upgrade past.
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

    def eligible_for_upgrade(self, project_name: NormalizedName) -> bool:
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
        way ``Factory.make_requirements_from_spec`` does, out of the spelling
        and the parent :meth:`note_dependency_spellings` recorded.
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
        """The template for a candidate that did not come from an explicit link.

        A constraint may carry the ``--hash`` lines for a requirement that
        carries none, and pip lets it: ``_iter_found_candidates`` copies the
        constraint's hash options onto the template, and
        ``make_install_req_from_link`` then writes the candidate's install
        requirement as ``name==version`` instead of repeating the unpinned
        requirement. That is what stops ``--require-hashes`` rejecting it as
        ``HashUnpinned``.

        Only the index path, because that is the only path pip does it on:
        a requirement naming a link keeps the template it was written from.
        The copy is what pip does too, and for the same reason: the
        requirement being copied is a root ireq shared with the requirement
        set, so setting the hash options on it would change the user's own
        requirement.
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


def _distinct_links(ireqs: Sequence[InstallRequirement]) -> int:
    """How many different URLs a project's explicit requirements name.

    ``links_equivalent`` rather than equality, because pip counts two
    spellings of one URL as one requirement.
    """
    distinct: list[Link] = []
    for ireq in ireqs:
        assert ireq.link is not None
        if not any(links_equivalent(link, ireq.link) for link in distinct):
            distinct.append(ireq.link)
    return len(distinct)


def _activating_extras(
    requirement: Requirement, provided: frozenset[NormalizedName]
) -> frozenset[NormalizedName]:
    """Which of the parent's extras switch this dependency on.

    Empty when the dependency applies with no extra requested, which is the
    same test pip's own metadata layer makes before it hands a requirement
    to the resolver.
    """
    marker = requirement.marker
    if marker is None or marker.evaluate({"extra": ""}):
        return frozenset()
    return frozenset(extra for extra in provided if marker.evaluate({"extra": extra}))


def _with_extras(requirement: Requirement, extras: frozenset[NormalizedName]) -> str:
    """``pkg[extra]==1.0``: one requirement, respelled with more extras."""
    merged = sorted({canonicalize_name(e) for e in requirement.extras} | extras)
    text = f"{requirement.name}[{','.join(merged)}]"
    if requirement.specifier:
        text += str(requirement.specifier)
    if requirement.url:
        text += f"@ {requirement.url}"
    if requirement.marker is not None:
        text += f" ; {requirement.marker}"
    return text


def _without_url(requirement: Requirement) -> str:
    """``pkg[extra] ; marker``, with the direct URL taken out.

    PEP 508 forbids a specifier alongside a URL, so nothing is lost but the
    link, and the link is now pip's. The extras and the marker have to
    survive: they are what decides whether the dependency applies at all.
    """
    text = requirement.name
    if requirement.extras:
        text += "[" + ",".join(sorted(requirement.extras)) + "]"
    if requirement.marker is not None:
        text += f" ; {requirement.marker}"
    return text
