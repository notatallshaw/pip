import collections
import math
from typing import (
    TYPE_CHECKING,
    Dict,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
    TypeVar,
    Union,
)
from typing import Any, Hashable, Optional, Set, List

from pip._internal.resolution.resolvelib.requirements import SpecifierRequirement
from pip._vendor.resolvelib.providers import AbstractProvider

from .base import Candidate, Constraint, Requirement
from .candidates import REQUIRES_PYTHON_IDENTIFIER
from .factory import Factory

if TYPE_CHECKING:
    from pip._vendor.resolvelib.providers import Preference
    from pip._vendor.resolvelib.resolvers import RequirementInformation

    PreferenceInformation = RequirementInformation[Requirement, Candidate]

    _ProviderBase = AbstractProvider[Requirement, Candidate, str]
else:
    _ProviderBase = AbstractProvider

# Notes on the relationship between the provider, the factory, and the
# candidate and requirement classes.
#
# The provider is a direct implementation of the resolvelib class. Its role
# is to deliver the API that resolvelib expects.
#
# Rather than work with completely abstract "requirement" and "candidate"
# concepts as resolvelib does, pip has concrete classes implementing these two
# ideas. The API of Requirement and Candidate objects are defined in the base
# classes, but essentially map fairly directly to the equivalent provider
# methods. In particular, `find_matches` and `is_satisfied_by` are
# requirement methods, and `get_dependencies` is a candidate method.
#
# The factory is the interface to pip's internal mechanisms. It is stateless,
# and is created by the resolver and held as a property of the provider. It is
# responsible for creating Requirement and Candidate objects, and provides
# services to those objects (access to pip's finder and preparer).


D = TypeVar("D")
V = TypeVar("V")


def _get_with_identifier(
    mapping: Mapping[str, V],
    identifier: str,
    default: D,
) -> Union[D, V]:
    """Get item from a package name lookup mapping with a resolver identifier.

    This extra logic is needed when the target mapping is keyed by package
    name, which cannot be directly looked up with an identifier (which may
    contain requested extras). Additional logic is added to also look up a value
    by "cleaning up" the extras from the identifier.
    """
    if identifier in mapping:
        return mapping[identifier]
    # HACK: Theoretically we should check whether this identifier is a valid
    # "NAME[EXTRAS]" format, and parse out the name part with packaging or
    # some regular expression. But since pip's resolver only spits out three
    # kinds of identifiers: normalized PEP 503 names, normalized names plus
    # extras, and Requires-Python, we can cheat a bit here.
    name, open_bracket, _ = identifier.partition("[")
    if open_bracket and name in mapping:
        return mapping[name]
    return default


class _OrderedStructure:
    """
    Represents a data structure based on a Directed Acyclic Graph (DAG)
    to maintain a strict ordering among elements of any hashable type.
    It allows for adding relationships between elements, ensures the
    order does not create cycles (which would violate strict ordering),
    and can perform topological sorts to determine the relative position
    of each element.
    """

    def __init__(self) -> None:
        """
        Initializes the OrderedStructure instance with an empty graph
        representation and a set of nodes.
        """
        self._graph: Dict[Hashable, Set[Hashable]] = {}
        self._nodes: Set[Hashable] = set()

    def add_relation(self, a: Hashable, b: Hashable) -> None:
        """
        Adds a directed relationship (a > b) between two elements,
        ensuring it doesn't violate the structure's strict ordering.

        :param Hashable a: The element that comes before b.
        :param Hashable b: The element that comes after a.
        :raises ValueError: If adding the relationship violates the
            strict ordering (creates a cycle).
        """
        if a in self._graph and self._has_path(b, a):
            raise ValueError(f"Adding {a} > {b} violates strict ordering.")
        self._graph.setdefault(a, set()).add(b)
        self._graph.setdefault(b, set())
        self._nodes.update([a, b])

    def _has_path(self, src: Hashable, dest: Hashable, visited: Optional[Set[Hashable]] = None) -> bool:
        """
        Helper method to check if there's a path from src to dest, used
        to detect cycles.

        :param Hashable src: The starting node.
        :param Hashable dest: The destination node.
        :param set[Hashable] visited: The set of nodes already visited
            during the path finding.
        :return: True if a path exists, False otherwise.
        :rtype: bool
        """
        if visited is None:
            visited = set()
        visited.add(src)
        for neighbor in self._graph.get(src, []):
            if neighbor == dest or (neighbor not in visited and self._has_path(neighbor, dest, visited)):
                return True
        return False

    def topological_sort(self) -> List[Hashable]:
        """
        Performs a topological sort on the elements based on their
        established ordering.

        :return: A list of elements in their topologically sorted order.
        :rtype: list[Hashable]
        """
        visited: Set[Hashable] = set()
        stack: list[Hashable] = []
        for node in list(self._nodes):
            if node not in visited:
                self._topological_sort_util(node, visited, stack)
        return stack[::-1]  # Return reversed stack for correct order

    def _topological_sort_util(self, node: Hashable, visited: Set[Hashable], stack: list[Hashable]) -> None:
        """
        Helper method for topological sorting, visiting nodes recursively.

        :param Hashable node: The current node being visited.
        :param set[Hashable] visited: Set of nodes that have been visited.
        :param list[Hashable] stack: The stack where the sorted nodes are collected.
        """
        visited.add(node)
        for neighbor in self._graph.get(node, []):
            if neighbor not in visited:
                self._topological_sort_util(neighbor, visited, stack)
        stack.append(node)

    def find_position(self, element: Hashable) -> int:
        """
        Finds the topological position (rank) of the given element based
        on the current ordering.

        :param Hashable element: The element to find the position of.
        :return: The position of the element in the topological sort, or
            -1 if the element does not exist.
        :rtype: int
        """
        sorted_elements = self.topological_sort()
        return sorted_elements.index(element) if element in sorted_elements else -1

    def element_exists(self, element: Hashable) -> bool:
        """
        Checks if the given element exists in the structure.

        :param Hashable element: The element to check.
        :return: True if the element exists, False otherwise.
        :rtype: bool
        """
        return element in self._nodes


class PipProvider(_ProviderBase):
    """Pip's provider implementation for resolvelib.

    :params constraints: A mapping of constraints specified by the user.
        Keys are canonicalized project names.
    :params ignore_dependencies: Whether the user specified ``--no-deps``.
    :params upgrade_strategy: The user-specified upgrade strategy.
    :params user_requested: A set of canonicalized package names that
        the user supplied for pip to install/upgrade.
    """

    def __init__(
        self,
        factory: Factory,
        constraints: Dict[str, Constraint],
        ignore_dependencies: bool,
        upgrade_strategy: str,
        user_requested: Dict[str, int],
    ) -> None:
        self._factory = factory
        self._constraints = constraints
        self._ignore_dependencies = ignore_dependencies
        self._upgrade_strategy = upgrade_strategy
        self._user_requested = user_requested
        self._known_depths: Dict[str, float] = collections.defaultdict(lambda: math.inf)

    def identify(self, requirement_or_candidate: Union[Requirement, Candidate]) -> str:
        return requirement_or_candidate.name

    def get_preference(
        self,
        identifier: str,
        resolutions: Mapping[str, Candidate],
        candidates: Mapping[str, Iterator[Candidate]],
        information: Mapping[str, Iterable["PreferenceInformation"]],
        backtrack_causes: Sequence["PreferenceInformation"],
    ) -> "Preference":
        """Produce a sort key for given requirement based on preference.

        The lower the return value is, the more preferred this group of
        arguments is.

        Currently pip considers the following in order:

        * Prefer if any of the known requirements is "direct", e.g. points to an
          explicit URL.
        * If equal, prefer if any requirement is "pinned", i.e. contains
          operator ``===`` or ``==``.
        * If equal, calculate an approximate "depth" and resolve requirements
          closer to the user-specified requirements first. If the depth cannot
          by determined (eg: due to no matching parents), it is considered
          infinite.
        * Order user-specified requirements by the order they are specified.
        * If equal, prefers "non-free" requirements, i.e. contains at least one
          operator, such as ``>=`` or ``<``.
        * If equal, order alphabetically for consistency (helps debuggability).
        """
        try:
            next(iter(information[identifier]))
        except StopIteration:
            # There is no information for this identifier, so there's no known
            # candidates.
            has_information = False
        else:
            has_information = True

        if has_information:
            lookups = (r.get_candidate_lookup() for r, _ in information[identifier])
            candidate, ireqs = zip(*lookups)
        else:
            candidate, ireqs = None, ()

        operators = [
            specifier.operator
            for specifier_set in (ireq.specifier for ireq in ireqs if ireq)
            for specifier in specifier_set
        ]

        direct = candidate is not None
        pinned = any(op[:2] == "==" for op in operators)
        unfree = bool(operators)

        try:
            requested_order: Union[int, float] = self._user_requested[identifier]
        except KeyError:
            requested_order = math.inf
            if has_information:
                parent_depths = (
                    self._known_depths[parent.name] if parent is not None else 0.0
                    for _, parent in information[identifier]
                )
                inferred_depth = min(d for d in parent_depths) + 1.0
            else:
                inferred_depth = math.inf
        else:
            inferred_depth = 1.0
        self._known_depths[identifier] = inferred_depth

        requested_order = self._user_requested.get(identifier, math.inf)

        # Requires-Python has only one candidate and the check is basically
        # free, so we always do it first to avoid needless work if it fails.
        requires_python = identifier == REQUIRES_PYTHON_IDENTIFIER

        # Prefer the causes of backtracking on the assumption that the problem
        # resolving the dependency tree is related to the failures that caused
        # the backtracking
        backtrack_cause = self.is_backtrack_cause(identifier, backtrack_causes)

        return (
            not requires_python,
            not direct,
            not pinned,
            not backtrack_cause,
            inferred_depth,
            requested_order,
            not unfree,
            identifier,
        )

    def find_matches(
        self,
        identifier: str,
        requirements: Mapping[str, Iterator[Requirement]],
        incompatibilities: Mapping[str, Iterator[Candidate]],
    ) -> Iterable[Candidate]:
        def _eligible_for_upgrade(identifier: str) -> bool:
            """Are upgrades allowed for this project?

            This checks the upgrade strategy, and whether the project was one
            that the user specified in the command line, in order to decide
            whether we should upgrade if there's a newer version available.

            (Note that we don't need access to the `--upgrade` flag, because
            an upgrade strategy of "to-satisfy-only" means that `--upgrade`
            was not specified).
            """
            if self._upgrade_strategy == "eager":
                return True
            elif self._upgrade_strategy == "only-if-needed":
                user_order = _get_with_identifier(
                    self._user_requested,
                    identifier,
                    default=None,
                )
                return user_order is not None
            return False

        constraint = _get_with_identifier(
            self._constraints,
            identifier,
            default=Constraint.empty(),
        )
        return self._factory.find_candidates(
            identifier=identifier,
            requirements=requirements,
            constraint=constraint,
            prefers_installed=(not _eligible_for_upgrade(identifier)),
            incompatibilities=incompatibilities,
        )

    def is_satisfied_by(self, requirement: Requirement, candidate: Candidate) -> bool:
        return requirement.is_satisfied_by(candidate)

    def get_dependencies(self, candidate: Candidate) -> Sequence[Requirement]:
        with_requires = not self._ignore_dependencies
        return [r for r in candidate.iter_dependencies(with_requires) if r is not None]

    @staticmethod
    def is_backtrack_cause(
        identifier: str, backtrack_causes: Sequence["PreferenceInformation"]
    ) -> bool:
        for backtrack_cause in backtrack_causes:
            if identifier == backtrack_cause.requirement.name:
                return True
            if backtrack_cause.parent and identifier == backtrack_cause.parent.name:
                return True
        return False

    def unpin_requirement(
        self,
        identifiers: Iterable[str],
        resolutions: Mapping[str, Candidate],
        candidates: Mapping[str, Iterator[Candidate]],
        information: Mapping[str, Iterable["PreferenceInformation"]],
        backtrack_causes: Sequence["PreferenceInformation"],
    ) -> Iterable[str]:
        """
        Return identifiers that are already pinned (not in identifiers)
        Resolvelib will backjump back to a state when it it not already pinned
        """
        if not backtrack_causes:
            return []

        # Check if any causes have requirements with upper bounds specifiers
        upper_bounded: set[str] = set()
        for cause in backtrack_causes:
            requirement = cause.requirement
            if not isinstance(requirement, SpecifierRequirement):
                continue

            for specifier in requirement.install_requirement.specifier:
                if specifier.operator in ("<", "<="):
                    upper_bounded.add(requirement.name)
                    upper_bounded.add(requirement.project_name)
                    break

        if not upper_bounded:
            return []

        requirements_to_unpin = upper_bounded - set(identifiers)
        if requirements_to_unpin:
            return requirements_to_unpin

        return []