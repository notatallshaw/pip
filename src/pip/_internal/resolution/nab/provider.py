"""Choose candidates and package priority from pip's installation options."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from functools import cache
from typing import TypeVar

from .base import Candidate, Constraint, Requirement
from .factory import Factory
from .requirements import ExplicitRequirement

Preference = tuple[bool, bool, bool, float, bool, str]


D = TypeVar("D")
V = TypeVar("V")


def _get_with_identifier(
    mapping: Mapping[str, V],
    identifier: str,
    default: D,
) -> D | V:
    """Look up a package policy by its identifier, then its name without extras."""
    if identifier in mapping:
        return mapping[identifier]
    # Identifiers contain normalized names, optional extras, or Requires-Python.
    name, open_bracket, _ = identifier.partition("[")
    if open_bracket and name in mapping:
        return mapping[name]
    return default


class PipProvider:
    """Apply upgrade, constraint, and dependency policies to native candidates."""

    def __init__(
        self,
        factory: Factory,
        constraints: dict[str, Constraint],
        ignore_dependencies: bool,
        upgrade_strategy: str,
        user_requested: dict[str, int],
    ) -> None:
        self._factory = factory
        self._constraints = constraints
        self._ignore_dependencies = ignore_dependencies
        self._upgrade_strategy = upgrade_strategy
        self._user_requested = user_requested

    def get_preference(
        self,
        identifier: str,
        requirements: Iterable[Requirement],
    ) -> Preference:
        """Produce a sort key for given requirement based on preference.

        The lower the return value is, the more preferred this group of
        arguments is.

        Currently pip considers the following in order:

        * Any requirement that is "direct", e.g., points to an explicit URL.
        * Any requirement that is "pinned", i.e., contains the operator ``===``
          or ``==`` without a wildcard.
        * Any requirement that imposes an upper version limit, i.e., contains the
          operator ``<``, ``<=``, ``~=``, or ``==`` with a wildcard. Because
          pip prioritizes the latest version, preferring explicit upper bounds
          can rule out infeasible candidates sooner. This does not imply that
          upper bounds are good practice; they can make dependency management
          and resolution harder.
        * Order user-specified requirements as they are specified, placing
          other requirements afterward.
        * Any "non-free" requirement, i.e., one that contains at least one
          operator, such as ``>=`` or ``!=``.
        * Alphabetical order for consistency (aids debuggability).
        """
        native_requirements = tuple(requirements)
        direct = any(
            isinstance(req, ExplicitRequirement) for req in native_requirements
        )
        ireqs = [req.get_candidate_lookup()[1] for req in native_requirements]

        operators: list[tuple[str, str]] = [
            (specifier.operator, specifier.version)
            for specifier_set in (ireq.specifier for ireq in ireqs if ireq)
            for specifier in specifier_set
        ]

        pinned = any(((op[:2] == "==") and ("*" not in ver)) for op, ver in operators)
        upper_bounded = any(
            ((op in ("<", "<=", "~=")) or (op == "==" and "*" in ver))
            for op, ver in operators
        )
        unfree = bool(operators)
        requested_order = self._user_requested.get(identifier, math.inf)

        return (
            not direct,
            not pinned,
            not upper_bounded,
            requested_order,
            not unfree,
            identifier,
        )

    def find_matches(
        self,
        identifier: str,
        requirements: Mapping[str, Iterable[Requirement]],
    ) -> Iterable[Candidate]:
        """Enumerate candidates admitted by active requirements and constraints."""
        constraint = _get_with_identifier(
            self._constraints,
            identifier,
            default=Constraint.empty(),
        )
        return self._factory.find_candidates(
            identifier=identifier,
            requirements=requirements,
            constraint=constraint,
            prefers_installed=(not self._eligible_for_upgrade(identifier)),
            is_satisfied_by=_is_satisfied_by,
        )

    def _eligible_for_upgrade(self, identifier: str) -> bool:
        """Allow upgrades for all packages, requested packages, or neither."""
        if self._upgrade_strategy == "eager":
            return True
        if self._upgrade_strategy == "only-if-needed":
            return (
                _get_with_identifier(self._user_requested, identifier, None) is not None
            )
        return False

    def get_dependencies(self, candidate: Candidate) -> Iterable[Requirement]:
        """Read metadata lazily, honoring --no-deps while checking Requires-Python."""
        with_requires = not self._ignore_dependencies
        # iter_dependencies() can perform nontrivial work so delay until needed.
        return (r for r in candidate.iter_dependencies(with_requires) if r is not None)


@cache
def _is_satisfied_by(requirement: Requirement, candidate: Candidate) -> bool:
    return requirement.is_satisfied_by(candidate)
