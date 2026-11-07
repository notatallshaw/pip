from __future__ import annotations

import contextlib
import copy
import functools
import logging
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import (
    NamedTuple,
    TypeVar,
)

from pip._vendor.nab_provider.callbacks import CandidateQuery, RequirementCause
from pip._vendor.nab_provider.candidate_stream import (
    CandidateGroupRejectedError,
    ordered_candidates,
)
from pip._vendor.packaging.requirements import InvalidRequirement
from pip._vendor.packaging.specifiers import SpecifierSet
from pip._vendor.packaging.utils import NormalizedName, canonicalize_name
from pip._vendor.packaging.version import InvalidVersion, Version
from pip._vendor.rich.markup import escape

from pip._internal.cache import CacheEntry, WheelCache
from pip._internal.exceptions import (
    DistributionNotFound,
    HashError,
    InstallationError,
    InvalidInstalledPackage,
    MetadataInconsistent,
    MetadataInvalid,
    UnsupportedPythonVersion,
    UnsupportedWheel,
)
from pip._internal.index.package_finder import PackageFinder
from pip._internal.metadata import BaseDistribution, get_default_environment
from pip._internal.models.link import Link, links_equivalent
from pip._internal.models.wheel import Wheel
from pip._internal.operations.prepare import RequirementPreparer
from pip._internal.req.constructors import (
    install_req_drop_extras,
    install_req_from_link_and_ireq,
)
from pip._internal.req.req_install import (
    InstallRequirement,
    check_invalid_constraint_type,
)
from pip._internal.resolution.base import InstallRequirementProvider
from pip._internal.utils.compatibility_tags import get_supported
from pip._internal.utils.hashes import Hashes
from pip._internal.utils.packaging import get_requirement, requirement_without_marker
from pip._internal.utils.virtualenv import running_under_virtualenv

from .base import (
    Candidate,
    Constraint,
    Requirement,
    intersect_hash_options,
    source_link_without_hashes,
    source_links_equivalent,
)
from .candidates import (
    AlreadyInstalledCandidate,
    BaseCandidate,
    EditableCandidate,
    ExtrasCandidate,
    LinkCandidate,
    RequiresPythonCandidate,
    as_base_candidate,
)

logger = logging.getLogger(__name__)

CacheCandidateType = TypeVar("CacheCandidateType")
Cache = dict[Link, CacheCandidateType]


class CollectedRootRequirements(NamedTuple):
    requirements: list[Requirement]
    constraints: dict[str, Constraint]
    user_requested: dict[str, int]


class Factory:
    def __init__(
        self,
        finder: PackageFinder,
        preparer: RequirementPreparer,
        make_install_req: InstallRequirementProvider,
        wheel_cache: WheelCache | None,
        use_user_site: bool,
        force_reinstall: bool,
        ignore_installed: bool,
        ignore_requires_python: bool,
        py_version_info: tuple[int, ...] | None = None,
    ) -> None:
        self._finder = finder
        self.preparer = preparer
        self._wheel_cache = wheel_cache
        self._python_candidate = RequiresPythonCandidate(py_version_info)
        self._make_install_req_from_spec = make_install_req
        self._use_user_site = use_user_site
        self._force_reinstall = force_reinstall
        self._ignore_requires_python = ignore_requires_python

        self._build_failures: Cache[InstallationError] = {}
        self._link_candidate_cache: dict[tuple[Link, bool], LinkCandidate] = {}
        self.input_options: dict[str, Constraint] = {}
        self._source_hash_checks: set[tuple[Link, str]] = set()
        self._invalid_versions: dict[str, set[Version]] = {}
        self._editable_candidate_cache: Cache[EditableCandidate] = {}
        self._installed_candidate_cache: dict[str, AlreadyInstalledCandidate] = {}
        self._extras_candidate_cache: dict[
            tuple[int, frozenset[NormalizedName]], ExtrasCandidate
        ] = {}
        self._supported_tags_cache = get_supported()

        if not ignore_installed:
            env = get_default_environment()
            self._installed_dists = {
                dist.canonical_name: dist
                for dist in env.iter_installed_distributions(local_only=False)
            }
        else:
            self._installed_dists = {}

    @property
    def force_reinstall(self) -> bool:
        return self._force_reinstall

    def _fail_if_link_is_unsupported_wheel(self, link: Link) -> None:
        if not link.is_wheel:
            return
        wheel = Wheel(link.filename)
        if wheel.supported(self._finder.target_python.get_unsorted_tags()):
            return
        msg = f"{link.filename} is not a supported wheel on this platform."
        raise UnsupportedWheel(msg)

    def _make_extras_candidate(
        self,
        base: BaseCandidate,
        extras: frozenset[str],
        *,
        comes_from: InstallRequirement | None = None,
    ) -> ExtrasCandidate:
        cache_key = (id(base), frozenset(canonicalize_name(e) for e in extras))
        try:
            candidate = self._extras_candidate_cache[cache_key]
        except KeyError:
            candidate = ExtrasCandidate(base, extras, comes_from=comes_from)
            self._extras_candidate_cache[cache_key] = candidate
        return candidate

    def _make_candidate_from_dist(
        self,
        dist: BaseDistribution,
        extras: frozenset[str],
        template: InstallRequirement,
    ) -> Candidate:
        try:
            base = self._installed_candidate_cache[dist.canonical_name]
        except KeyError:
            base = AlreadyInstalledCandidate(dist, template, factory=self)
            self._installed_candidate_cache[dist.canonical_name] = base
        if not extras:
            return base
        return self._make_extras_candidate(base, extras, comes_from=template)

    def _make_candidate_from_link(
        self,
        link: Link,
        extras: frozenset[str],
        template: InstallRequirement,
        name: NormalizedName | None,
        version: Version | None,
    ) -> Candidate | None:
        base: BaseCandidate | None = self._make_base_candidate_from_link(
            link, template, name, version
        )
        if not extras or base is None:
            return base
        return self._make_extras_candidate(base, extras, comes_from=template)

    def _make_base_candidate_from_link(
        self,
        link: Link,
        template: InstallRequirement,
        name: NormalizedName | None,
        version: Version | None,
    ) -> BaseCandidate | None:
        # TODO: Check already installed candidate, and use it if the link and
        # editable flag match.

        name, template = self._input_hash_template(link, template, name)
        options = (
            self.input_options.get(name, Constraint.empty())
            if name is not None
            else Constraint.empty()
        )
        if options.links:
            for editable in self._editable_candidate_cache.values():
                if editable.source_link is not None and source_links_equivalent(
                    link, editable.source_link
                ):
                    self._check_input_hashes(editable, template)
                    return editable
        if link in self._build_failures:
            # We already tried this candidate before, and it does not build.
            # Don't bother trying again.
            return None

        if template.editable:
            if link not in self._editable_candidate_cache:
                try:
                    self._editable_candidate_cache[link] = EditableCandidate(
                        link,
                        template,
                        factory=self,
                        name=name,
                        version=version,
                    )
                except (MetadataInconsistent, MetadataInvalid) as e:
                    logger.info(
                        "Discarding [blue underline]%s[/]: [yellow]%s[reset]",
                        escape(str(link)),
                        escape(str(e)),
                        extra={"markup": True},
                    )
                    self._build_failures[link] = e
                    return None

            editable = self._editable_candidate_cache[link]
            self._check_input_hashes(editable, template)
            self._check_source_hash(editable, link)
            return editable
        else:
            cache_key = source_link_without_hashes(link), template.is_direct
            if cache_key not in self._link_candidate_cache:
                try:
                    self._link_candidate_cache[cache_key] = LinkCandidate(
                        link,
                        template,
                        factory=self,
                        name=name,
                        version=version,
                    )
                except MetadataInconsistent as e:
                    logger.info(
                        "Discarding [blue underline]%s[/]: [yellow]%s[reset]",
                        escape(str(link)),
                        escape(str(e)),
                        extra={"markup": True},
                    )
                    self._build_failures[link] = e
                    return None
            candidate = self._link_candidate_cache[cache_key]
            self._check_input_hashes(candidate, template)
            self._check_source_hash(candidate, link)
            return candidate

    def _input_hash_template(
        self,
        link: Link,
        template: InstallRequirement,
        name: NormalizedName | None,
    ) -> tuple[NormalizedName | None, InstallRequirement]:
        """Return a source name and preparation template with its input hashes."""
        if name is None and not template.hash_options:
            matching = [
                package
                for package, options in self.input_options.items()
                if options.hash_options
                and options.links
                and all(
                    source_links_equivalent(link, required)
                    for required in options.links
                )
            ]
            if len(matching) == 1:
                name = canonicalize_name(matching[0])
        options = self.input_options.get(name) if name is not None else None
        if options is None or not options.hash_options:
            return name, template
        if options.links and not all(
            source_links_equivalent(link, required) for required in options.links
        ):
            return name, template
        hash_options = intersect_hash_options(
            options.hash_options, template.hash_options
        )
        original = template.original_link
        narrow_original = (
            template.user_supplied
            and isinstance(original, Link)
            and original.hash is not None
            and original.hash_name is not None
            and not Hashes(hash_options).is_hash_allowed(
                original.hash_name, original.hash.lower()
            )
        )
        if hash_options != template.hash_options or narrow_original:
            template = copy.copy(template)
            template.hash_options = hash_options
            if narrow_original:
                assert isinstance(original, Link)
                template.original_link = source_link_without_hashes(original)
                if template.link is original:
                    template.link = template.original_link
        return name, template

    def _check_input_hashes(
        self,
        candidate: LinkCandidate | EditableCandidate,
        template: InstallRequirement,
    ) -> None:
        """Check additional input hashes discovered with an archive's package name."""
        source_link = candidate.source_link
        assert source_link is not None, "Prepared candidates retain their source"

        _, wanted = self._input_hash_template(
            source_link, template, candidate.project_name
        )
        prepared = candidate.get_install_requirement()
        if not wanted.hash_options:
            return
        if source_link.is_vcs or source_link.is_existing_dir():
            return

        hashes = Hashes(wanted.hash_options)
        if self._native_input_hashes_known(prepared, wanted, hashes):
            return

        info = prepared.download_info
        if (
            prepared.is_wheel_from_cache
            and info is not None
            and info.archive_info is not None
            and info.archive_info.hashes is not None
            and hashes.has_one_of(info.archive_info.hashes)
        ):
            return

        if prepared.local_file_path is None:
            self.preparer.prepare_linked_requirements_more([prepared])
        assert prepared.local_file_path is not None
        prepared.needs_more_preparation = False

        try:
            hashes.check_against_path(prepared.local_file_path)
        except HashError as error:
            error.req = wanted
            raise

    def _native_input_hashes_known(
        self, prepared: InstallRequirement, wanted: InstallRequirement, hashes: Hashes
    ) -> bool:
        """Whether native preparation enforces the complete input checksum set."""
        if wanted.hash_options != prepared.hash_options:
            return False
        link = prepared.link
        if self.preparer.require_hashes:
            link = (
                prepared.original_link
                if prepared.is_direct and prepared.user_supplied
                else None
            )
        if not isinstance(link, Link) or link.hash is None:
            return True
        return link.hash_name is not None and hashes.is_hash_allowed(
            link.hash_name, link.hash.lower()
        )

    def _check_source_hash(self, candidate: Candidate, link: Link) -> None:
        """Check a dependency checksum against the prepared candidate's archive."""
        if candidate.source_link is not None and links_equivalent(
            candidate.source_link, link
        ):
            return

        hashes = link.as_hashes()
        if not hashes:
            return

        prepared = candidate.get_install_requirement()
        if (
            prepared is None
            or prepared.link is None
            or not source_links_equivalent(prepared.link, link)
        ):
            return

        # Metadata alone cannot establish an additional archive checksum.
        archive_path = prepared.local_file_path
        if archive_path is None and prepared.needs_more_preparation:
            try:
                self.preparer.prepare_linked_requirements_more([prepared])
            except HashError as error:
                error.req = prepared
                raise

            archive_path = prepared.local_file_path

            # The completion helper can leave this flag set.
            if archive_path is not None:
                prepared.needs_more_preparation = False

        if archive_path is None:
            return

        key = link, archive_path
        if key in self._source_hash_checks:
            return

        try:
            hashes.check_against_path(archive_path)
        except HashError as error:
            error.req = prepared
            raise

        self._source_hash_checks.add(key)
        return

    def _iter_found_candidates(
        self,
        ireqs: Sequence[InstallRequirement],
        specifier: SpecifierSet,
        hashes: Hashes,
        prefers_installed: bool,
        incompatible_candidates: set[Candidate],
        constraint_hash_options: dict[str, list[str]] | None = None,
        *,
        resolve_installed_source: Callable[[Candidate], Candidate] | None = None,
    ) -> CandidateQuery[Candidate]:
        if not ireqs:
            return CandidateQuery(())
        query = CandidateQuery[Candidate](())

        # The InstallRequirement implementation requires us to give it a
        # "template". Here we just choose the first requirement to represent
        # all of them.
        # Hopefully the Project model can correct this mismatch in the future.
        template = ireqs[0]
        assert template.req, "Candidates found on index must be PEP 508"
        if (
            constraint_hash_options
            and not template.hash_options
            and any(constraint_hash_options.values())
        ):
            template = copy.copy(template)
            template.hash_options = {
                k: list(v) for k, v in constraint_hash_options.items()
            }
        assert template.req  # to prevent mypy from being confused by the copy
        name = canonicalize_name(template.req.name)

        extras: frozenset[str] = frozenset()
        for ireq in ireqs:
            assert ireq.req, "Candidates found on index must be PEP 508"
            specifier &= ireq.req.specifier
            hashes &= ireq.hashes(trust_internet=False)
            extras |= frozenset(ireq.extras)

        def _get_installed_candidate() -> Candidate | None:
            """Get the candidate for the currently-installed version."""
            # If --force-reinstall is set, we want the version from the index
            # instead, so we "pretend" there is nothing installed.
            if self._force_reinstall:
                return None
            try:
                installed_dist = self._installed_dists[name]
            except KeyError:
                return None

            try:
                # Don't use the installed distribution if its version
                # does not fit the current dependency graph.
                if not specifier.contains(installed_dist.version, prereleases=True):
                    return None
            except InvalidVersion as e:
                raise InvalidInstalledPackage(dist=installed_dist, invalid_exc=e)

            candidate = self._make_candidate_from_dist(
                dist=installed_dist,
                extras=extras,
                template=template,
            )
            if resolve_installed_source is not None:
                candidate = resolve_installed_source(candidate)
                if not specifier.contains(candidate.version, prereleases=True):
                    return None
            # The candidate is a known incompatibility. Don't use it.
            if candidate in incompatible_candidates:
                return None
            return candidate

        def iter_index_candidate_infos() -> (
            Iterator[tuple[Version, Callable[[], Candidate | None]]]
        ):
            artifacts = [
                artifact
                for artifact in self._finder.find_all_candidates(name)
                if artifact.version not in self._invalid_versions.get(name, ())
            ]
            # PEP 592: Yanked releases are ignored unless the specifier
            # explicitly pins a version (via '==' or '===') that can be
            # solely satisfied by a yanked release.

            def is_pinned(specifier: SpecifierSet) -> bool:
                for sp in specifier:
                    if sp.operator == "===":
                        return True
                    if sp.operator != "==":
                        continue
                    if sp.version.endswith(".*"):
                        continue
                    return True
                return False

            pinned = is_pinned(specifier)
            withdrawn_matches = any(
                artifact.link.is_yanked
                and specifier.contains(artifact.version, prereleases=True)
                for artifact in artifacts
            )
            if not pinned:
                query.may_expand = withdrawn_matches
                rejected = {
                    candidate.source_link
                    for candidate in (
                        *self._link_candidate_cache.values(),
                        *self._extras_candidate_cache.values(),
                    )
                    if candidate in incompatible_candidates
                }
                # Keep live fallback separate from unpermitted withdrawn files.
                artifacts = [
                    artifact
                    for artifact in artifacts
                    if not artifact.link.is_yanked
                    and (not withdrawn_matches or artifact.link not in rejected)
                ]
            evaluator = self._finder.make_candidate_evaluator(
                project_name=name, specifier=specifier, hashes=hashes
            )
            icans = evaluator.get_applicable_candidates(artifacts)
            if any(
                artifact.version.is_prerelease
                and specifier.contains(artifact.version, prereleases=True)
                for artifact in artifacts
            ):
                query.may_expand = True
            # PackageFinder returns earlier versions first, so we reverse.
            for ican in reversed(icans):
                func = functools.partial(
                    self._make_candidate_from_link,
                    link=ican.link,
                    extras=extras,
                    template=template,
                    name=name,
                    version=ican.version,
                )
                yield (
                    ican.version,
                    functools.partial(
                        self._prepare_index_candidate, ican.version, func
                    ),
                )

        installed = _get_installed_candidate()
        query.candidates = ordered_candidates(
            iter_index_candidate_infos(),
            incumbent=(installed.version, installed) if installed is not None else None,
            prefer_incumbent=prefers_installed,
            precedes=lambda installed, available: installed >= available,
            excluded=incompatible_candidates,
        )
        return query

    def _prepare_index_candidate(
        self, version: Version, prepare: Callable[[], Candidate | None]
    ) -> Candidate | None:
        try:
            return prepare()
        except MetadataInvalid as error:
            assert error.ireq.name is not None
            self._invalid_versions.setdefault(error.ireq.name, set()).add(version)
            logger.warning(
                "Ignoring version %s of %s since it has invalid metadata:\n%s\n"
                "Please use pip<24.1 if you need to use this version.",
                version,
                error.ireq.name,
                error,
            )
            raise CandidateGroupRejectedError from error

    def _iter_explicit_candidates_from_base(
        self,
        base_requirements: Iterable[Requirement],
        extras: frozenset[str],
    ) -> Iterator[Candidate]:
        """Produce explicit candidates from the base given an extra-ed package.

        :param base_requirements: Requirements known to the resolver. The
            requirements are guaranteed to not have extras.
        :param extras: The extras to inject into the explicit requirements'
            candidates.
        """
        for req in base_requirements:
            lookup_cand, _ = req.get_candidate_lookup()
            if lookup_cand is None:  # Not explicit.
                continue
            # We've stripped extras from the identifier, and should always
            # get a BaseCandidate here, unless there's a bug elsewhere.
            base_cand = as_base_candidate(lookup_cand)
            assert base_cand is not None, "no extras here"
            yield self._make_extras_candidate(base_cand, extras)

    def _iter_candidates_from_constraints(
        self,
        identifier: str,
        constraint: Constraint,
        template: InstallRequirement,
    ) -> Iterator[Candidate]:
        """Produce explicit candidates from constraints.

        This creates "fake" InstallRequirement objects that are basically clones
        of what "should" be the template, but with original_link set to link.
        """
        extras: frozenset[str] = frozenset()
        base_identifier = identifier
        with contextlib.suppress(InvalidRequirement):
            parsed_requirement = get_requirement(identifier)
            if parsed_requirement.name != identifier:
                base_identifier = canonicalize_name(parsed_requirement.name)
                extras = frozenset(parsed_requirement.extras)

        for link in constraint.links:
            self._fail_if_link_is_unsupported_wheel(link)
            base_candidate = self._make_base_candidate_from_link(
                link,
                template=install_req_from_link_and_ireq(link, template),
                name=canonicalize_name(base_identifier),
                version=None,
            )
            if base_candidate is None:
                continue
            if extras:
                yield self._make_extras_candidate(base_candidate, extras)
            else:
                yield base_candidate

    def find_candidates(
        self,
        identifier: str,
        requirements: Mapping[str, Iterable[Requirement]],
        incompatibilities: Mapping[str, Iterator[Candidate]],
        constraint: Constraint,
        prefers_installed: bool,
        is_satisfied_by: Callable[[Requirement, Candidate], bool],
        *,
        resolve_installed_source: Callable[[Candidate], Candidate] | None = None,
    ) -> CandidateQuery[Candidate]:
        # Collect basic lookup information from the requirements.
        explicit_candidates: set[Candidate] = set()
        ireqs: list[InstallRequirement] = []
        for req in requirements[identifier]:
            cand, ireq = req.get_candidate_lookup()
            if cand is not None:
                explicit_candidates.add(cand)
            if ireq is not None:
                ireqs.append(ireq)

        # If the current identifier contains extras, add requires and explicit
        # candidates from entries from extra-less identifier.
        with contextlib.suppress(InvalidRequirement):
            parsed_requirement = get_requirement(identifier)
            if parsed_requirement.name != identifier:
                explicit_candidates.update(
                    self._iter_explicit_candidates_from_base(
                        requirements.get(parsed_requirement.name, ()),
                        frozenset(parsed_requirement.extras),
                    ),
                )
                for req in requirements.get(parsed_requirement.name, []):
                    _, ireq = req.get_candidate_lookup()
                    if ireq is not None:
                        ireqs.append(ireq)

        # Add explicit candidates from constraints. We only do this if there are
        # known ireqs, which represent requirements not already explicit. If
        # there are no ireqs, we're constraining already-explicit requirements,
        # which is handled later when we return the explicit candidates.
        if ireqs:
            try:
                explicit_candidates.update(
                    self._iter_candidates_from_constraints(
                        identifier,
                        constraint,
                        template=ireqs[0],
                    ),
                )
            except UnsupportedWheel:
                # If we're constrained to install a wheel incompatible with the
                # target architecture, no candidates will ever be valid.
                return CandidateQuery(())

        incompatible_candidates = set(incompatibilities.get(identifier, ()))

        # If none of the requirements want an explicit candidate, we can ask
        # the finder for candidates.
        if not explicit_candidates:
            if constraint.links:
                return CandidateQuery(())
            return self._iter_found_candidates(
                ireqs,
                constraint.specifier,
                constraint.hashes,
                prefers_installed,
                incompatible_candidates,
                constraint.hash_options,
                resolve_installed_source=resolve_installed_source,
            )

        return CandidateQuery(
            c
            for c in explicit_candidates
            if c not in incompatible_candidates
            and constraint.is_satisfied_by(c)
            and all(is_satisfied_by(req, c) for req in requirements[identifier])
        )

    def _make_requirements_from_install_req(
        self,
        ireq: InstallRequirement,
        requested_extras: Iterable[str],
        *,
        parent: Candidate | None = None,
    ) -> Iterator[Requirement]:
        """
        Returns requirement objects associated with the given InstallRequirement. In
        most cases this will be a single object but the following special cases exist:
            - the InstallRequirement has markers that do not apply -> result is empty
            - the InstallRequirement has both a constraint (or link) and extras
                -> result is split in two requirement objects: one with the constraint
                (or link) and one with the extra. This allows centralized constraint
                handling for the base, resulting in fewer candidate rejections.
        """
        if not ireq.match_markers(requested_extras):
            logger.info(
                "Ignoring %s: markers '%s' don't match your environment",
                ireq.name,
                ireq.markers,
            )
        elif not ireq.link:
            if ireq.extras and ireq.req is not None and ireq.req.specifier:
                yield Requirement.from_install_req(install_req_drop_extras(ireq))
            yield Requirement.from_install_req(ireq)
        else:
            options = (
                self.input_options.get(canonicalize_name(ireq.name), Constraint.empty())
                if ireq.name is not None
                else Constraint.empty()
            )
            conflicting_source = any(
                not source_links_equivalent(ireq.link, link) for link in options.links
            )
            if conflicting_source and not ireq.user_supplied:
                assert ireq.name is not None
                yield Requirement.unavailable(
                    canonicalize_name(ireq.name), source_requirement=ireq
                )
                return

            indexed_parent = (
                parent.base if isinstance(parent, ExtrasCandidate) else parent
            )
            if (
                isinstance(indexed_parent, LinkCandidate)
                and not indexed_parent.get_install_requirement().is_direct
                and indexed_parent.project_name == ireq.name
                and indexed_parent.source_link is not None
                and source_links_equivalent(indexed_parent.source_link, ireq.link)
            ):
                self._check_source_hash(indexed_parent, ireq.link)
                yield self.make_requirement_from_candidate(indexed_parent)
                if ireq.extras:
                    yield self.make_requirement_from_candidate(
                        self._make_extras_candidate(
                            indexed_parent, frozenset(ireq.extras)
                        )
                    )
                return

            source = (
                ireq.link
                if conflicting_source
                else next(iter(options.links), ireq.link)
            )
            template = install_req_from_link_and_ireq(source, ireq)
            self._fail_if_link_is_unsupported_wheel(source)
            # Always make the link candidate for the base requirement to make it
            # available to `find_candidates` for explicit candidate lookup for any
            # set of extras.
            # The extras are required separately via a second requirement.
            cand = self._make_base_candidate_from_link(
                source,
                template=install_req_drop_extras(template) if ireq.extras else template,
                name=canonicalize_name(ireq.name) if ireq.name else None,
                version=None,
            )
            if cand is None:
                # There's no way we can satisfy a URL requirement if the underlying
                # candidate fails to build. An unnamed URL must be user-supplied, so
                # we fail eagerly. If the URL is named, an unsatisfiable requirement
                # can make the resolver do the right thing, either backtrack (and
                # maybe find some other requirement that's buildable) or raise a
                # ResolutionImpossible eventually.
                if not ireq.name:
                    raise self._build_failures[ireq.link]
                yield Requirement.unavailable(canonicalize_name(ireq.name))
            else:
                self._check_source_hash(cand, ireq.link)
                # require the base from the link
                yield self.make_requirement_from_candidate(cand)
                if ireq.extras:
                    # require the extras on top of the base candidate
                    yield self.make_requirement_from_candidate(
                        self._make_extras_candidate(cand, frozenset(ireq.extras))
                    )

    def _collect_constraints(
        self, root_ireqs: list[InstallRequirement]
    ) -> dict[str, Constraint]:
        """Collect active input constraints before any root source is prepared."""
        constraints: dict[str, Constraint] = {}
        for ireq in root_ireqs:
            if ireq.constraint:
                # Ensure we only accept valid constraints
                problem = check_invalid_constraint_type(ireq)
                if problem:
                    raise InstallationError(problem)
                if not ireq.match_markers():
                    continue
                assert ireq.name, "Constraint must be named"
                name = canonicalize_name(ireq.name)
                if name in constraints:
                    constraints[name] &= ireq
                else:
                    constraints[name] = Constraint.from_ireq(ireq)
        return constraints

    def _set_input_options(
        self,
        root_ireqs: list[InstallRequirement],
        constraints: dict[str, Constraint],
    ) -> None:
        """Retain fixed hash policies and discard incomplete hash-mode preparations."""
        self.input_options = dict(constraints)
        for ireq in root_ireqs:
            if not ireq.constraint and ireq.name is not None and ireq.match_markers():
                name = canonicalize_name(ireq.name)
                self.input_options[name] = (
                    self.input_options.get(name, Constraint.empty()) & ireq
                )
        if self.preparer.require_hashes:
            self._link_candidate_cache = {
                key: candidate
                for key, candidate in self._link_candidate_cache.items()
                if candidate.get_install_requirement().local_file_path is not None
            }

    def collect_root_requirements(
        self, root_ireqs: list[InstallRequirement]
    ) -> CollectedRootRequirements:
        constraints = self._collect_constraints(root_ireqs)
        self._set_input_options(root_ireqs, constraints)
        collected = CollectedRootRequirements([], constraints, {})
        for i, ireq in enumerate(root_ireqs):
            if not ireq.constraint:
                reqs = list(
                    self._make_requirements_from_install_req(
                        ireq,
                        requested_extras=(),
                    )
                )
                if not reqs:
                    continue
                template = reqs[0]
                if ireq.name is None and ireq.link is not None:
                    name = template.project_name
                    named = copy.copy(ireq)
                    named.req = get_requirement(name)
                    self.input_options[name] = (
                        self.input_options.get(name, Constraint.empty()) & named
                    )
                if (
                    ireq.user_supplied
                    and template.project_name not in collected.user_requested
                ):
                    collected.user_requested[template.project_name] = i
                collected.requirements.extend(reqs)
        # Admit base constraints before their extras. The stable sort preserves
        # declaration order within each group for native failure diagnostics.
        collected.requirements.sort(key=lambda r: r.name != r.project_name)
        return collected

    def make_requirement_from_candidate(self, candidate: Candidate) -> Requirement:
        return Requirement.from_candidate(candidate)

    def make_requirements_from_spec(
        self,
        specifier: str,
        comes_from: InstallRequirement | None,
        requested_extras: Iterable[str] = (),
        *,
        parent: Candidate | None = None,
    ) -> Iterator[Requirement]:
        """
        Returns requirement objects associated with the given specifier. In most cases
        this will be a single object but the following special cases exist:
            - the specifier has markers that do not apply -> result is empty
            - the specifier has both a constraint and extras -> result is split
                in two requirement objects: one with the constraint and one with the
                extra. This allows centralized constraint handling for the base,
                resulting in fewer candidate rejections.
        """
        ireq = self._make_install_req_from_spec(specifier, comes_from)
        return self._make_requirements_from_install_req(
            ireq, requested_extras, parent=parent
        )

    def has_installed(self, package: str) -> bool:
        """Check whether a base or extras package has an installed distribution."""
        return package.partition("[")[0] in self._installed_dists

    def installed_self_candidate(self, candidate: Candidate) -> Candidate:
        """Use an installation's own source before reading obsolete dependencies."""
        base = candidate.base if isinstance(candidate, ExtrasCandidate) else candidate
        assert isinstance(base, AlreadyInstalledCandidate)
        extras = (
            candidate.extras if isinstance(candidate, ExtrasCandidate) else frozenset()
        )
        try:
            for dependency in base.dist.iter_dependencies(extras):
                if (
                    canonicalize_name(dependency.name) != base.project_name
                    or dependency.url is None
                ):
                    continue
                requirements = self.make_requirements_from_spec(
                    str(dependency), base.get_install_requirement(), extras, parent=base
                )
                for requirement in requirements:
                    replacement, _ = requirement.get_candidate_lookup()
                    if replacement is None:
                        continue
                    selected = as_base_candidate(replacement)
                    if selected is None:
                        continue
                    return (
                        self._make_extras_candidate(selected, extras)
                        if extras
                        else selected
                    )
        except InvalidRequirement as error:
            raise InvalidInstalledPackage(dist=base.dist, invalid_exc=error) from None
        return candidate

    def make_requires_python_requirement(
        self,
        specifier: SpecifierSet,
    ) -> Requirement | None:
        if self._ignore_requires_python:
            return None
        # Don't bother creating a dependency for an empty Requires-Python.
        if not str(specifier):
            return None
        return Requirement.for_python(specifier, self._python_candidate)

    def get_wheel_cache_entry(self, link: Link, name: str | None) -> CacheEntry | None:
        """Look up the link in the wheel cache.

        If ``preparer.require_hashes`` is True, don't use the wheel cache,
        because cached wheels, always built locally, have different hashes
        than the files downloaded from the index server and thus throw false
        hash mismatches. Furthermore, cached wheels at present have
        nondeterministic contents due to file modification times.
        """
        if self._wheel_cache is None:
            return None
        return self._wheel_cache.get_cache_entry(
            link=link,
            package_name=name,
            supported_tags=self._supported_tags_cache,
        )

    def replaces_installed_metadata(self, candidate: Candidate) -> bool:
        """Whether an index selection supersedes prepared installed metadata."""
        installed = self._installed_candidate_cache.get(candidate.project_name)
        requirement = candidate.get_install_requirement()
        return (
            installed is not None
            and requirement is not None
            and not requirement.is_direct
            and installed.version == candidate.version
        )

    def get_dist_to_uninstall(self, candidate: Candidate) -> BaseDistribution | None:
        # TODO: Are there more cases this needs to return True? Editable?
        dist = self._installed_dists.get(candidate.project_name)
        if dist is None:  # Not installed, no uninstallation required.
            return None

        # We're installing into global site. The current installation must
        # be uninstalled, no matter it's in global or user site, because the
        # user site installation has precedence over global.
        if not self._use_user_site:
            return dist

        # We're installing into user site. Remove the user site installation.
        if dist.in_usersite:
            return dist

        # We're installing into user site, but the installed incompatible
        # package is in global site. We can't uninstall that, and would let
        # the new user installation to "shadow" it. But shadowing won't work
        # in virtual environments, so we error out.
        if running_under_virtualenv() and dist.in_site_packages:
            message = (
                f"Will not install to the user site because it will lack "
                f"sys.path precedence to {dist.raw_name} in {dist.location}"
            )
            raise InstallationError(message)
        return None

    def _report_requires_python_error(
        self, causes: Sequence[tuple[SpecifierSet, Candidate]]
    ) -> UnsupportedPythonVersion:
        assert causes, "Requires-Python error reported with no cause"

        version = self._python_candidate.version

        if len(causes) == 1:
            specifier = str(causes[0][0])
            message = (
                f"Package {causes[0][1].name!r} requires a different "
                f"Python: {version} not in {specifier!r}"
            )
            return UnsupportedPythonVersion(message)

        message = f"Packages require a different Python. {version} not in:"
        for requirement, parent in causes:
            package = parent.format_for_error()
            specifier = str(requirement)
            message += f"\n{specifier!r} (required by {package})"
        return UnsupportedPythonVersion(message)

    def _report_single_requirement_conflict(
        self, req: Requirement, parent: Candidate | None
    ) -> DistributionNotFound:
        _, ireq = req.get_candidate_lookup()
        if ireq is not None and ireq.req is not None:
            req_str = str(requirement_without_marker(ireq.req))
        else:
            req_str = str(req)
        req_disp = req_str if parent is None else f"{req_str} (from {parent.name})"
        cands = self._finder.find_all_candidates(req.project_name)
        skipped_by_requires_python = self._finder.requires_python_skipped_reasons()

        versions_set: set[Version] = set()
        yanked_versions_set: set[Version] = set()
        for c in cands:
            is_yanked = c.link.is_yanked if c.link else False
            if is_yanked:
                yanked_versions_set.add(c.version)
            else:
                versions_set.add(c.version)

        versions = [str(v) for v in sorted(versions_set)]
        yanked_versions = [str(v) for v in sorted(yanked_versions_set)]

        if yanked_versions:
            # Saying "version X is yanked" isn't entirely accurate.
            # https://github.com/pypa/pip/issues/11745#issuecomment-1402805842
            logger.critical(
                "Ignored the following yanked versions: %s",
                ", ".join(yanked_versions) or "none",
            )
        if skipped_by_requires_python:
            logger.critical(
                "Ignored the following versions that require a different python "
                "version: %s",
                "; ".join(skipped_by_requires_python) or "none",
            )

        # Check if only final releases are allowed for this package
        version_type = "version"
        allows_pre = None
        if self._finder.release_control is not None:
            allows_pre = self._finder.release_control.allows_prereleases(
                canonicalize_name(req.project_name)
            )
            if allows_pre is False:
                version_type = "final version"

        if len(cands) == 1 and cands[0].locked:
            # The package finder ensures that requirements from pylock files
            # have exactly one candidate. So we can provide a specific error
            # message in this case.
            if cands[0].version.is_prerelease and allows_pre is False:
                logger.critical(
                    "A pre-release version %s is specified in a provided lock file "
                    "for %s but only final versions are allowed",
                    cands[0].version,
                    cands[0].name,
                )
            else:
                logger.critical(
                    "The requirement %s is not compatible with "
                    "version %s specified in a provided lock file",
                    req_disp,
                    ", ".join(versions),
                )
        else:
            logger.critical(
                "Could not find a %s that satisfies the requirement %s "
                "(from versions: %s)",
                version_type,
                req_disp,
                ", ".join(versions) or "none",
            )
        if str(req) == "requirements.txt":
            logger.info(
                "HINT: You are attempting to install a package literally "
                'named "requirements.txt" (which cannot exist). Consider '
                "using the '-r' flag to install the packages listed in "
                "requirements.txt"
            )
        return DistributionNotFound(f"No matching distribution found for {req_disp}")

    def _has_any_candidates(self, project_name: str) -> bool:
        """Check known sources and index listings without preparing more metadata."""
        name = project_name.partition("[")[0]
        return (
            name in self._installed_dists
            or any(
                candidate.project_name == name
                for candidate in (
                    *self._link_candidate_cache.values(),
                    *self._editable_candidate_cache.values(),
                )
            )
            or bool(self._finder.find_all_candidates(name))
        )

    def get_installation_error(
        self,
        requirement_causes: Sequence[RequirementCause[Requirement, Candidate]],
        constraints: dict[str, Constraint],
    ) -> InstallationError:
        assert requirement_causes, "Installation error reported with no cause"

        # If one of the things we can't solve is "we need Python X.Y",
        # that is what we report.
        requires_python_causes: list[tuple[SpecifierSet, Candidate]] = []
        for cause in requirement_causes:
            requirement = cause.requirement
            if requirement.is_python and not requirement.is_satisfied_by(
                self._python_candidate
            ):
                assert requirement.specifier is not None and cause.parent is not None
                requires_python_causes.append((requirement.specifier, cause.parent))
        if requires_python_causes:
            return self._report_requires_python_error(requires_python_causes)

        # A lock fixes the available version even when it also supplies a root.
        for cause in requirement_causes:
            req = cause.requirement
            if req.ireq is None or not req.ireq.specifier:
                continue
            candidates = self._finder.find_all_candidates(req.project_name)
            if (
                len(candidates) == 1
                and candidates[0].locked
                and not req.ireq.specifier.contains(
                    candidates[0].version, prereleases=True
                )
            ):
                return self._report_single_requirement_conflict(req, cause.parent)

        # Otherwise, we have a set of causes which can't all be satisfied
        # at once.

        # The simplest case is when we have *one* cause that can't be
        # satisfied. We just report that case.
        if len(requirement_causes) == 1:
            cause = next(iter(requirement_causes))
            req, parent = cause.requirement, cause.parent
            if req.name not in constraints:
                return self._report_single_requirement_conflict(req, parent)

        # OK, we now have a list of requirements that can't all be
        # satisfied at once.

        # A couple of formatting helpers
        def text_join(parts: list[str]) -> str:
            if len(parts) == 1:
                return parts[0]

            return ", ".join(parts[:-1]) + " and " + parts[-1]

        def describe_trigger(parent: Candidate) -> str:
            ireq = parent.get_install_requirement()
            if not ireq or not ireq.comes_from:
                return f"{parent.name}=={parent.version}"
            if isinstance(ireq.comes_from, InstallRequirement):
                return str(ireq.comes_from.name)
            return str(ireq.comes_from)

        triggers = set()
        for cause in requirement_causes:
            req, parent = cause.requirement, cause.parent
            if parent is None:
                # This is a root requirement, so we can report it directly
                trigger = req.format_for_error()
            else:
                trigger = describe_trigger(parent)
            triggers.add(trigger)

        if triggers:
            info = text_join(sorted(triggers))
        else:
            info = "the requested packages"

        msg = (
            f"Cannot install {info} because these package versions "
            "have conflicting dependencies."
        )
        logger.critical(msg)
        msg = "\nThe conflict is caused by:"

        relevant_constraints = set()
        for cause in requirement_causes:
            req, parent = cause.requirement, cause.parent
            if req.name in constraints:
                relevant_constraints.add(req.name)
            msg = msg + "\n    "
            if parent:
                msg = msg + f"{parent.name} {parent.version} depends on "
            else:
                msg = msg + "The user requested "
            msg = msg + req.format_for_error()
        for key in relevant_constraints:
            constraint_text = f"{key}{constraints[key].format_for_error()}"
            msg += f"\n    The user requested (constraint) {constraint_text}"

        # Check for causes that had no candidates
        causes = set()
        for cause in requirement_causes:
            req = cause.requirement
            causes.add(req.name)

        no_candidates = {c for c in causes if not self._has_any_candidates(c)}
        if no_candidates:
            msg = (
                msg
                + "\n\n"
                + "Additionally, some packages in these conflicts have no "
                + "matching distributions available for your environment:"
                + "\n    "
                + "\n    ".join(sorted(no_candidates))
            )

        msg = (
            msg
            + "\n\n"
            + "To fix this you could try to:\n"
            + "1. loosen the range of package versions you've specified\n"
            + "2. remove package versions to allow pip to attempt to solve "
            + "the dependency conflict\n"
        )

        logger.info(msg)

        return DistributionNotFound(
            "ResolutionImpossible: for help visit "
            "https://pip.pypa.io/en/latest/topics/dependency-resolution/"
            "#dealing-with-dependency-conflicts"
        )
