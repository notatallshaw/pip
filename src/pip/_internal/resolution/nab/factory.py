from __future__ import annotations

import contextlib
import copy
import logging
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from typing import (
    TYPE_CHECKING,
    NamedTuple,
    Protocol,
    TypeVar,
    cast,
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
from pip._internal.models.candidate import InstallationCandidate
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
    RequirementCause,
    SourceDomainChanged,
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
from .requirements import (
    ExplicitRequirement,
    RequiresPythonRequirement,
    SpecifierRequirement,
    SpecifierWithoutExtrasRequirement,
    UnsatisfiableRequirement,
)

if TYPE_CHECKING:

    class ConflictCause(Protocol):
        requirement: RequiresPythonRequirement
        parent: Candidate


logger = logging.getLogger(__name__)

CacheValueType = TypeVar("CacheValueType")
Cache = dict[Link, CacheValueType]


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
        self.catalogue_only = False
        self._catalogue_hash_checks: set[tuple[Link, str]] = set()
        self._input_options: dict[str, Constraint] = {}
        self.observed_sources: dict[NormalizedName, list[BaseCandidate]] = {}
        self.observed_source_keys: set[tuple[Link, bool, bool]] = set()
        self.input_source_keys: set[tuple[Link, bool, bool]] = set()
        self.observed_self_sources: set[tuple[Link, bool, bool]] = set()
        self.source_epoch = 0

        self._build_failures: Cache[InstallationError] = {}
        self._link_candidate_cache: dict[tuple[Link, bool], LinkCandidate] = {}
        self._editable_candidate_cache: Cache[EditableCandidate] = {}
        self._installed_candidate_cache: dict[str, AlreadyInstalledCandidate] = {}
        self._extras_candidate_cache: dict[
            tuple[int, frozenset[NormalizedName]], ExtrasCandidate
        ] = {}
        self._supported_tags_cache = get_supported()

        if not ignore_installed:
            env = get_default_environment()
            self._installed_dists: dict[NormalizedName, BaseDistribution] = {
                dist.canonical_name: dist
                for dist in env.iter_installed_distributions(local_only=False)
            }
        else:
            self._installed_dists = {}

    @property
    def force_reinstall(self) -> bool:
        return self._force_reinstall

    @contextlib.contextmanager
    def catalogue_context(self) -> Generator[None]:
        """Scope source discovery and transient failures to one resolution."""
        assert not self.catalogue_only
        failures = self._build_failures
        self._build_failures = {}
        self.catalogue_only = True
        try:
            yield
        finally:
            self.catalogue_only = False
            self._catalogue_hash_checks.clear()
            self._build_failures = failures

    def _fail_if_link_is_unsupported_wheel(self, link: Link) -> None:
        if not link.is_wheel:
            return
        wheel = Wheel(link.filename)
        if wheel.supported(self._finder.target_python.get_unsorted_tags()):
            return
        msg = f"{link.filename} is not a supported wheel on this platform."
        raise UnsupportedWheel(msg)

    def make_extras_candidate(
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
        return self.make_extras_candidate(base, extras, comes_from=template)

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
        return self.make_extras_candidate(base, extras, comes_from=template)

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
            return editable
        else:
            cache_key = link, template.is_direct
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
                for package, options in self._input_options.items()
                if options.hash_options
                and options.links
                and all(
                    source_links_equivalent(link, required)
                    for required in options.links
                )
            ]
            if len(matching) == 1:
                name = canonicalize_name(matching[0])
        options = self._input_options.get(name) if name is not None else None
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

    def installed_candidate_info(
        self, identifier: str, template: InstallRequirement | None = None
    ) -> tuple[Version, Callable[[], Candidate]] | None:
        """Probe the version now and defer native preparation until selection."""
        if self._force_reinstall:
            return None
        requirement = get_requirement(identifier)
        dist = self._installed_dists.get(canonicalize_name(requirement.name))
        if dist is None:
            return None
        try:
            version = dist.version
        except InvalidVersion as error:
            raise InvalidInstalledPackage(dist=dist, invalid_exc=error) from error

        def prepare() -> Candidate:
            """Use the existing native cache without preparing during the probe."""
            prepared_template = template
            if prepared_template is None:
                prepared_template = self._make_install_req_from_spec(identifier, None)
            return self._make_candidate_from_dist(
                dist, frozenset(requirement.extras), prepared_template
            )

        return version, prepare

    def index_candidates(
        self, identifier: str, hashes: Hashes | None = None
    ) -> list[InstallationCandidate]:
        """List finder-ordered artifacts without preparing their metadata."""
        requirement = get_requirement(identifier)
        name = canonicalize_name(requirement.name)
        if hashes is None:
            hashes = self._input_options.get(name, Constraint.empty()).hashes
        # SpecifierSet equality ignores the prerelease override in finder caches.
        evaluator = self._finder.make_candidate_evaluator(
            project_name=name, specifier=SpecifierSet(prereleases=True), hashes=hashes
        )
        candidates = evaluator.get_applicable_candidates(
            self._finder.find_all_candidates(name)
        )
        return list(reversed(candidates))

    def prepare_index_candidate(
        self,
        identifier: str,
        artifact: InstallationCandidate,
        template: InstallRequirement | None = None,
    ) -> Candidate | None:
        """Prepare the chosen artifact with the original native cache and options."""
        requirement = get_requirement(identifier)
        if template is None:
            template = self._make_install_req_from_spec(identifier, None)
        return self._make_candidate_from_link(
            artifact.link,
            frozenset(requirement.extras),
            template,
            canonicalize_name(requirement.name),
            artifact.version,
        )

    @staticmethod
    def candidate_source_key(candidate: Candidate) -> tuple[Link, bool, bool] | None:
        """Identify acquisition mode separately from a cached wheel fetch link."""
        base = (
            candidate.base
            if isinstance(candidate, ExtrasCandidate)
            else as_base_candidate(candidate)
        )
        if base is None:
            return None

        link = base.source_link
        if link is None:
            return None

        requirement = base.get_install_requirement()
        assert requirement is not None
        return link, base.is_editable, requirement.is_direct

    def seed_source(self, candidate: Candidate) -> None:
        """Retain an input source without making the registry an admission rule."""
        base = (
            candidate.base
            if isinstance(candidate, ExtrasCandidate)
            else as_base_candidate(candidate)
        )
        if base is None:
            return
        key = self.candidate_source_key(base)
        if key is not None and key not in self.observed_source_keys:
            self.observed_source_keys.add(key)
            self.observed_sources.setdefault(base.project_name, []).append(base)
        if key is not None:
            self.input_source_keys.add(key)

    def catalogue_prerelease_policy(self, identifier: str) -> bool | None:
        """Return the fixed command-line release policy for a catalogue package."""
        control = self._finder.release_control
        if control is None:
            return None
        name = canonicalize_name(get_requirement(identifier).name)
        return control.allows_prereleases(name)

    def catalogue_prerelease_admitted(
        self,
        candidate: Candidate,
        requirements: Mapping[str, Iterable[Requirement]],
        constraint: Constraint,
    ) -> bool:
        """Check prerelease admission without synthetic validation pins."""
        if candidate.source_link is None:
            return True
        declarations = list(requirements[candidate.name])
        if candidate.name != candidate.project_name:
            declarations.extend(requirements.get(candidate.project_name, ()))
        specifier = constraint.specifier
        for requirement in declarations:
            _, ireq = requirement.get_candidate_lookup()
            if ireq is not None:
                specifier &= ireq.specifier
        result = self._finder.find_best_candidate(
            project_name=candidate.project_name,
            specifier=specifier,
            hashes=constraint.hashes,
        )
        return any(
            links_equivalent(item.link, candidate.source_link)
            for item in result.applicable_candidates
        )

    def candidates_from_constraints(
        self,
        identifier: str,
        constraint: Constraint,
        template: InstallRequirement,
    ) -> Iterator[Candidate]:
        """Prepare constraint links using the request's provenance and options."""
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
                yield self.make_extras_candidate(base_candidate, extras)
            else:
                yield base_candidate

    def _catalogue_constraint_conflict(
        self, ireq: InstallRequirement
    ) -> NormalizedName | None:
        """Return the package name when a URL violates an input constraint."""
        link = ireq.link
        if ireq.name is None or link is None:
            return None
        name = canonicalize_name(ireq.name)
        links = self._input_options.get(name, Constraint.empty()).links
        if any(not source_links_equivalent(link, required) for required in links):
            return name
        return None

    def _catalogue_link_hash_known(
        self, ireq: InstallRequirement, candidate: Candidate
    ) -> bool:
        """Check a dependency checksum against the prepared candidate's archive."""
        assert ireq.link is not None
        if candidate.source_link is not None and links_equivalent(
            candidate.source_link, ireq.link
        ):
            return True

        hashes = ireq.link.as_hashes()
        if not hashes:
            return True

        prepared = candidate.get_install_requirement()
        if (
            prepared is None
            or prepared.link is None
            or not source_links_equivalent(prepared.link, ireq.link)
        ):
            return False

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
            return False

        key = ireq.link, archive_path
        if key in self._catalogue_hash_checks:
            return True

        try:
            hashes.check_against_path(archive_path)
        except HashError as error:
            error.req = ireq
            raise

        self._catalogue_hash_checks.add(key)
        return True

    def _self_source_for_requirement(
        self, ireq: InstallRequirement, parent: Candidate | None
    ) -> LinkCandidate | None:
        """Bind an indexed candidate's own URL to that same prepared artifact."""
        assert ireq.name is not None and ireq.link is not None
        if isinstance(parent, ExtrasCandidate):
            parent = parent.base
        if not isinstance(parent, LinkCandidate):
            return None
        if (
            parent.get_install_requirement().is_direct
            or canonicalize_name(ireq.name) != parent.project_name
        ):
            return None

        source = parent.source_link
        if source is None or not source_links_equivalent(source, ireq.link):
            return None
        if not self._catalogue_link_hash_known(ireq, parent):
            return None

        return parent

    def _known_source_for_requirement(
        self, ireq: InstallRequirement, parent: Candidate | None
    ) -> LinkCandidate | EditableCandidate | None:
        """Reuse a prepared source after checking location, mode and hashes."""
        assert ireq.name is not None and ireq.link is not None
        alias = self._self_source_for_requirement(ireq, parent)
        if alias is not None:
            return alias

        name = canonicalize_name(ireq.name)
        inputs = self._input_options.get(name, Constraint.empty())

        # An input URL is known even before its first metadata preparation.
        if inputs.links and not any(
            self.candidate_source_key(known) in self.input_source_keys
            for known in self.observed_sources.get(name, ())
        ):
            for known in self.candidates_from_constraints(name, inputs, ireq):
                self.seed_source(known)

        for known in self.observed_sources.get(name, ()):
            key = self.candidate_source_key(known)
            if (
                key is not None
                and (
                    key in self.input_source_keys
                    or (key[1] == ireq.editable and key[2] == ireq.is_direct)
                )
                and source_links_equivalent(key[0], ireq.link)
                and self._catalogue_link_hash_known(ireq, known)
            ):
                assert isinstance(known, (LinkCandidate, EditableCandidate))
                return known

        return None

    def _record_observed_source(
        self, candidate: BaseCandidate, ireq: InstallRequirement
    ) -> None:
        """Record an optional source and invalidate the current query."""
        assert candidate.source_link is not None
        key = candidate.source_link, candidate.is_editable, ireq.is_direct
        if key in self.observed_source_keys:
            return

        self.observed_source_keys.add(key)
        self.observed_sources.setdefault(candidate.project_name, []).append(candidate)
        parent = ireq.comes_from
        if (
            isinstance(parent, InstallRequirement)
            and parent.satisfied_by is not None
            and parent.name == ireq.name
        ):
            self.observed_self_sources.add(key)

        self.source_epoch += 1
        raise SourceDomainChanged("observed candidate source")

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
        fixed_candidate: LinkCandidate | EditableCandidate | None = None
        new_source = False
        if (
            self.catalogue_only
            and ireq.link is not None
            and ireq.match_markers(requested_extras)
        ):
            conflict = self._catalogue_constraint_conflict(ireq)
            if conflict is not None:
                yield UnsatisfiableRequirement(conflict, source_requirement=ireq)
                return
            fixed_candidate = self._known_source_for_requirement(ireq, parent)
            new_source = fixed_candidate is None

        if not ireq.match_markers(requested_extras):
            logger.info(
                "Ignoring %s: markers '%s' don't match your environment",
                ireq.name,
                ireq.markers,
            )
        elif not ireq.link:
            if ireq.extras and ireq.req is not None and ireq.req.specifier:
                yield SpecifierWithoutExtrasRequirement(ireq)
            yield SpecifierRequirement(ireq)
        else:
            self._fail_if_link_is_unsupported_wheel(ireq.link)
            cand = (
                fixed_candidate
                if fixed_candidate is not None
                else self._make_base_candidate_from_link(
                    ireq.link,
                    template=install_req_drop_extras(ireq) if ireq.extras else ireq,
                    name=canonicalize_name(ireq.name) if ireq.name else None,
                    version=None,
                )
            )
            if cand is not None and new_source:
                self._record_observed_source(cand, ireq)

            if cand is None:
                # An unnamed URL is a root input, so a build failure is immediate.
                if not ireq.name:
                    raise self._build_failures[ireq.link]
                yield UnsatisfiableRequirement(canonicalize_name(ireq.name))
            else:
                # require the base from the link
                yield self.make_requirement_from_candidate(cand)
                if ireq.extras:
                    # require the extras on top of the base candidate
                    yield self.make_requirement_from_candidate(
                        self.make_extras_candidate(cand, frozenset(ireq.extras))
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
        self._input_options = dict(constraints)
        for ireq in root_ireqs:
            if not ireq.constraint and ireq.name is not None and ireq.match_markers():
                name = canonicalize_name(ireq.name)
                self._input_options[name] = (
                    self._input_options.get(name, Constraint.empty()) & ireq
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
                if ireq.user_supplied and template.name not in collected.user_requested:
                    collected.user_requested[template.name] = i
                collected.requirements.extend(reqs)
        # Admit base constraints before their extras. The stable sort preserves
        # declaration order within each group for native failure diagnostics.
        collected.requirements.sort(key=lambda r: r.name != r.project_name)
        return collected

    def make_requirement_from_candidate(
        self, candidate: Candidate
    ) -> ExplicitRequirement:
        return ExplicitRequirement(candidate)

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

    def make_requires_python_requirement(
        self,
        specifier: SpecifierSet,
    ) -> Requirement | None:
        if self._ignore_requires_python:
            return None
        # Don't bother creating a dependency for an empty Requires-Python.
        if not str(specifier):
            return None
        return RequiresPythonRequirement(specifier, self._python_candidate)

    def get_wheel_cache_entry(self, link: Link, name: str | None) -> CacheEntry | None:
        """Look up a cached wheel matching the link, package and supported tags."""
        if self._wheel_cache is None:
            return None

        return self._wheel_cache.get_cache_entry(
            link=link,
            package_name=name,
            supported_tags=self._supported_tags_cache,
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
        self, causes: Sequence[ConflictCause]
    ) -> UnsupportedPythonVersion:
        assert causes, "Requires-Python error reported with no cause"

        version = self._python_candidate.version

        if len(causes) == 1:
            specifier = str(causes[0].requirement.specifier)
            message = (
                f"Package {causes[0].parent.name!r} requires a different "
                f"Python: {version} not in {specifier!r}"
            )
            return UnsupportedPythonVersion(message)

        message = f"Packages require a different Python. {version} not in:"
        for cause in causes:
            package = cause.parent.format_for_error()
            specifier = str(cause.requirement.specifier)
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
        return False

    def get_input_conflict_error(
        self, causes: Sequence[RequirementCause], constraints: dict[str, Constraint]
    ) -> InstallationError:
        """Render mandatory input conflicts with lock-file-specific diagnostics."""
        project = causes[0].requirement.project_name
        if self._finder.has_locked_link(project):
            candidates = self._finder.find_all_candidates(project)
            if len(candidates) == 1:
                for requirement, parent in causes:
                    _, ireq = requirement.get_candidate_lookup()
                    if ireq is not None and not ireq.specifier.contains(
                        candidates[0].version, prereleases=True
                    ):
                        return self._report_single_requirement_conflict(
                            requirement, parent
                        )
        return self.get_installation_error(causes, constraints)

    def get_installation_error(
        self,
        requirement_causes: Sequence[RequirementCause],
        constraints: dict[str, Constraint],
    ) -> InstallationError:
        """Render conflicting native requirements with their original parents."""
        assert requirement_causes, "Installation error reported with no cause"

        # If one of the things we can't solve is "we need Python X.Y",
        # that is what we report.
        requires_python_causes = [
            cause
            for cause in requirement_causes
            if isinstance(cause.requirement, RequiresPythonRequirement)
            and not cause.requirement.is_satisfied_by(self._python_candidate)
        ]
        if requires_python_causes:
            # The comprehension above makes sure all Requirement instances are
            # RequiresPythonRequirement, so let's cast for convenience.
            return self._report_requires_python_error(
                cast("Sequence[ConflictCause]", requires_python_causes),
            )

        # Otherwise, we have a set of causes which can't all be satisfied
        # at once.

        # The simplest case is when we have *one* cause that can't be
        # satisfied. We just report that case.
        if len(requirement_causes) == 1:
            req, parent = next(iter(requirement_causes))
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
        for req, parent in requirement_causes:
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
        for req, parent in requirement_causes:
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
        for req, _ in requirement_causes:
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
