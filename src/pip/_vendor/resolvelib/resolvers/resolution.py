from __future__ import annotations

import collections
import itertools
import operator
from typing import TYPE_CHECKING, Generic

from ..structs import (
    CT,
    KT,
    RT,
    DirectedGraph,
    IterableView,
    IteratorMapping,
    RequirementInformation,
    State,
    build_iter_view,
)
from .abstract import AbstractResolver, Result
from .criterion import Criterion
from .exceptions import (
    InconsistentCandidate,
    RequirementsConflicted,
    ResolutionImpossible,
    ResolutionTooDeep,
    ResolverException,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Mapping

    from ..providers import AbstractProvider, Preference
    from ..reporters import BaseReporter

_OPTIMISTIC_BACKJUMPING_RATIO: float = 0.1


def _build_result(state: State[RT, CT, KT]) -> Result[RT, CT, KT]:
    mapping = state.mapping
    all_keys: dict[int, KT | None] = {id(v): k for k, v in mapping.items()}
    all_keys[id(None)] = None

    graph: DirectedGraph[KT | None] = DirectedGraph()
    graph.add(None)  # Sentinel as root dependencies' parent.

    connected: set[KT | None] = {None}
    for key, criterion in state.criteria.items():
        if not _has_route_to_root(state.criteria, key, all_keys, connected):
            continue
        if key not in graph:
            graph.add(key)
        for p in criterion.iter_parent():
            try:
                pkey = all_keys[id(p)]
            except KeyError:
                continue
            if pkey not in graph:
                graph.add(pkey)
            graph.connect(pkey, key)

    return Result(
        mapping={k: v for k, v in mapping.items() if k in connected},
        graph=graph,
        criteria=state.criteria,
    )


class Resolution(Generic[RT, CT, KT]):
    """Stateful resolution object.

    This is designed as a one-off object that holds information to kick start
    the resolution process, and holds the results afterwards.
    """

    def __init__(
        self,
        provider: AbstractProvider[RT, CT, KT],
        reporter: BaseReporter[RT, CT, KT],
    ) -> None:
        self._p = provider
        self._r = reporter
        self._states: list[State[RT, CT, KT]] = []

        # Optimistic backjumping variables
        self._optimistic_backjumping_ratio = _OPTIMISTIC_BACKJUMPING_RATIO
        self._save_states: list[State[RT, CT, KT]] | None = None
        self._optimistic_start_round: int | None = None

        # Nogood learning: track combinations of candidates that lead to
        # conflicts so we can skip them before expensive provider calls.
        # Each entry is a frozenset of (identifier, semantic_id) pairs.
        # Only active when the provider implements get_candidate_semantic_id.
        self._learned_conflicts: set[frozenset[tuple[KT, object]]] = set()
        self._has_semantic_ids: bool | None = None

    @property
    def state(self) -> State[RT, CT, KT]:
        try:
            return self._states[-1]
        except IndexError as e:
            raise AttributeError("state") from e

    def _push_new_state(self) -> None:
        """Push a new state into history.

        This new state will be used to hold resolution results of the next
        coming round.
        """
        base = self._states[-1]
        state = State(
            mapping=base.mapping.copy(),
            criteria=base.criteria.copy(),
            backtrack_causes=base.backtrack_causes[:],
        )
        self._states.append(state)

    def _supports_nogood_learning(self) -> bool:
        """Check if the provider supports nogood learning via semantic IDs."""
        if self._has_semantic_ids is None:
            # Probe the first candidate in the mapping
            for v in self.state.mapping.values():
                self._has_semantic_ids = (
                    self._p.get_candidate_semantic_id(v) is not NotImplemented
                )
                break
            else:
                self._has_semantic_ids = False
        return self._has_semantic_ids

    def _get_current_decisions_hashed(self) -> frozenset[tuple[KT, object]]:
        """Get the current set of decisions as a hashable key."""
        return frozenset(
            (k, self._p.get_candidate_semantic_id(v))
            for k, v in self.state.mapping.items()
        )

    def _find_relevant_decisions(
        self, causes: list[RequirementInformation[RT, CT]]
    ) -> list[tuple[KT, object]]:
        """Find the minimal set of decisions relevant to the conflict.

        This implements a simplified version of First UIP (Unique Implication Point)
        conflict analysis from CDCL. We identify the packages that directly
        contributed to the conflict through their requirements.

        The minimal conflict clause includes only the packages whose requirements
        directly caused the conflict - NOT the package that couldn't be satisfied.

        For example, if package A requires X >= 1.0 and package B requires X < 1.0,
        the conflict clause should be {A, B}, not {A, B, X}. This is because
        the conflict exists regardless of which X version is tried.

        :param causes: RequirementInformation objects describing the conflict.
        :return: List of (identifier, candidate_semantic_id) pairs for relevant decisions.
        """
        result: list[tuple[KT, object]] = []
        seen_ids: set[KT] = set()

        # Add packages that directly caused the conflict (parents from causes)
        # These might be pinned decisions OR candidates being tried
        for cause in causes:
            if cause.parent is not None:
                parent_id = self._p.identify(cause.parent)
                if parent_id not in seen_ids:
                    seen_ids.add(parent_id)
                    # Use the parent from the cause, which has the correct version
                    parent_semantic = self._p.get_candidate_semantic_id(cause.parent)
                    result.append((parent_id, parent_semantic))

        # Don't include the unsatisfied package - the conflict exists regardless
        # of which version of that package is tried

        return result

    def _learn_conflict(
        self, causes: list[RequirementInformation[RT, CT]]
    ) -> None:
        """Learn a conflict clause from the current state (CDCL Learn rule).

        When a conflict is detected, this method records the minimal set of
        pinned candidates that led to this conflict. This allows us to skip
        re-exploring the same state in the future.

        This implements the CDCL "Learn" rule:
        https://en.wikipedia.org/wiki/Conflict-driven_clause_learning#Formalization

        We learn only the **relevant decisions** - packages directly involved
        in the conflict plus their immediate parents. This produces smaller,
        more general conflict clauses that can prune larger portions of the
        search space.

        This method should only be called when CDCL mode is enabled.

        :param causes: RequirementInformation objects describing the conflict.
        """
        # Use minimal conflict clause (inspired by First UIP analysis)
        # This learns only decisions relevant to the conflict, not all decisions
        relevant_decisions = self._find_relevant_decisions(causes)

        if len(relevant_decisions) >= 2:  # Only learn non-trivial conflicts
            conflict_clause = frozenset(relevant_decisions)
            self._learned_conflicts.add(conflict_clause)

    def _would_conflict_with_learned(self, name: KT, candidate: CT) -> bool:
        """Check if pinning this candidate would recreate a learned conflict.

        This is analogous to CDCL's unit propagation and conflict detection.
        Before trying a candidate, we check if it would result in a state
        that we've already proven to be conflicting.

        We check if any learned conflict:
        1. Contains this (name, candidate) pair
        2. Has all its OTHER decisions already present in current state

        If both conditions are met, trying this candidate would recreate
        the conflict.

        Reference: Conflict detection in CDCL
        https://en.wikipedia.org/wiki/Conflict-driven_clause_learning#Formalization

        This method should only be called when CDCL mode is enabled.

        :param name: The identifier being pinned.
        :param candidate: The candidate being considered.
        :return: True if this would recreate a known conflict.
        """
        if not self._learned_conflicts:
            return False

        candidate_semantic_id = self._p.get_candidate_semantic_id(candidate)
        candidate_decision = (name, candidate_semantic_id)

        # Get current decisions with semantic IDs
        current_decisions = self._get_current_decisions_hashed()

        # Check if any learned conflict would be completed by this candidate
        for learned_conflict in self._learned_conflicts:
            if candidate_decision not in learned_conflict:
                continue
            # Check if all OTHER decisions in the conflict are already present
            other_decisions = learned_conflict - {candidate_decision}
            if other_decisions <= current_decisions:
                # All other decisions are present, adding this candidate
                # would complete the learned conflict
                return True

        return False

    def _add_to_criteria(
        self,
        criteria: dict[KT, Criterion[RT, CT]],
        requirement: RT,
        parent: CT | None,
    ) -> None:
        self._r.adding_requirement(requirement=requirement, parent=parent)

        identifier = self._p.identify(requirement_or_candidate=requirement)
        criterion = criteria.get(identifier)
        if criterion:
            incompatibilities = list(criterion.incompatibilities)
        else:
            incompatibilities = []

        matches = self._p.find_matches(
            identifier=identifier,
            requirements=IteratorMapping(
                criteria,
                operator.methodcaller("iter_requirement"),
                {identifier: [requirement]},
            ),
            incompatibilities=IteratorMapping(
                criteria,
                operator.attrgetter("incompatibilities"),
                {identifier: incompatibilities},
            ),
        )

        if criterion:
            information = list(criterion.information)
            information.append(RequirementInformation(requirement, parent))
        else:
            information = [RequirementInformation(requirement, parent)]

        criterion = Criterion(
            candidates=build_iter_view(matches),
            information=information,
            incompatibilities=incompatibilities,
        )
        if not criterion.candidates:
            raise RequirementsConflicted(criterion)
        criteria[identifier] = criterion

    def _remove_information_from_criteria(
        self, criteria: dict[KT, Criterion[RT, CT]], parents: Collection[KT]
    ) -> None:
        """Remove information from parents of criteria.

        Concretely, removes all values from each criterion's ``information``
        field that have one of ``parents`` as provider of the requirement.

        :param criteria: The criteria to update.
        :param parents: Identifiers for which to remove information from all criteria.
        """
        if not parents:
            return
        for key, criterion in criteria.items():
            criteria[key] = Criterion(
                criterion.candidates,
                [
                    information
                    for information in criterion.information
                    if (
                        information.parent is None
                        or self._p.identify(information.parent) not in parents
                    )
                ],
                criterion.incompatibilities,
            )

    def _get_preference(self, name: KT) -> Preference:
        return self._p.get_preference(
            identifier=name,
            resolutions=self.state.mapping,
            candidates=IteratorMapping(
                self.state.criteria,
                operator.attrgetter("candidates"),
            ),
            information=IteratorMapping(
                self.state.criteria,
                operator.attrgetter("information"),
            ),
            backtrack_causes=self.state.backtrack_causes,
        )

    def _is_current_pin_satisfying(
        self, name: KT, criterion: Criterion[RT, CT]
    ) -> bool:
        try:
            current_pin = self.state.mapping[name]
        except KeyError:
            return False
        return all(
            self._p.is_satisfied_by(requirement=r, candidate=current_pin)
            for r in criterion.iter_requirement()
        )

    def _get_updated_criteria(self, candidate: CT) -> dict[KT, Criterion[RT, CT]]:
        criteria = self.state.criteria.copy()
        for requirement in self._p.get_dependencies(candidate=candidate):
            self._add_to_criteria(criteria, requirement, parent=candidate)
        return criteria

    def _attempt_to_pin_criterion(self, name: KT) -> list[Criterion[RT, CT]]:
        criterion = self.state.criteria[name]

        causes: list[Criterion[RT, CT]] = []
        skipped_by_learning: list[CT] = []
        for candidate in criterion.candidates:
            # Skip candidates that would recreate a learned conflict.
            # This avoids expensive provider calls for known-bad combinations.
            if self._learned_conflicts and self._would_conflict_with_learned(name, candidate):
                skipped_by_learning.append(candidate)
                continue

            try:
                criteria = self._get_updated_criteria(candidate)
            except RequirementsConflicted as e:
                self._r.rejecting_candidate(e.criterion, candidate)
                causes.append(e.criterion)
                continue

            # Check the newly-pinned candidate actually works. This should
            # always pass under normal circumstances, but in the case of a
            # faulty provider, we will raise an error to notify the implementer
            # to fix find_matches() and/or is_satisfied_by().
            satisfied = all(
                self._p.is_satisfied_by(requirement=r, candidate=candidate)
                for r in criterion.iter_requirement()
            )
            if not satisfied:
                raise InconsistentCandidate(candidate, criterion)

            self._r.pinning(candidate=candidate)
            self.state.criteria.update(criteria)

            # Put newly-pinned candidate at the end. This is essential because
            # backtracking looks at this mapping to get the last pin.
            self.state.mapping.pop(name, None)
            self.state.mapping[name] = candidate

            return []

        # If no candidate worked but we skipped some due to learned conflicts,
        # retry them. The learned conflict may have been overly conservative,
        # or the context may have changed. This ensures learning is purely
        # additive (never makes resolution worse than without learning).
        for candidate in skipped_by_learning:
            try:
                criteria = self._get_updated_criteria(candidate)
            except RequirementsConflicted as e:
                self._r.rejecting_candidate(e.criterion, candidate)
                causes.append(e.criterion)
                continue

            satisfied = all(
                self._p.is_satisfied_by(requirement=r, candidate=candidate)
                for r in criterion.iter_requirement()
            )
            if not satisfied:
                raise InconsistentCandidate(candidate, criterion)

            self._r.pinning(candidate=candidate)
            self.state.criteria.update(criteria)

            self.state.mapping.pop(name, None)
            self.state.mapping[name] = candidate

            return []

        # All candidates tried, nothing works. This criterion is a dead
        # end, signal for backtracking.

        return causes

    def _patch_criteria(
        self, incompatibilities_from_broken: list[tuple[KT, list[CT]]]
    ) -> bool:
        # Create a new state from the last known-to-work one, and apply
        # the previously gathered incompatibility information.
        for k, incompatibilities in incompatibilities_from_broken:
            if not incompatibilities:
                continue
            try:
                criterion = self.state.criteria[k]
            except KeyError:
                continue
            matches = self._p.find_matches(
                identifier=k,
                requirements=IteratorMapping(
                    self.state.criteria,
                    operator.methodcaller("iter_requirement"),
                ),
                incompatibilities=IteratorMapping(
                    self.state.criteria,
                    operator.attrgetter("incompatibilities"),
                    {k: incompatibilities},
                ),
            )
            candidates: IterableView[CT] = build_iter_view(matches)
            if not candidates:
                return False
            incompatibilities.extend(criterion.incompatibilities)
            self.state.criteria[k] = Criterion(
                candidates=candidates,
                information=list(criterion.information),
                incompatibilities=incompatibilities,
            )
        return True

    def _compute_backjump_target(
        self, incompatible_deps: set[KT]
    ) -> int | None:
        """Compute a backjump target based on when conflicting packages were pinned.

        Finds the second-highest state index where a conflicting package was
        first pinned. This lets us skip states that are irrelevant to the
        conflict. Returns None if we can't compute a useful target (fewer
        than 2 conflicting packages found in the state stack).
        """
        if len(incompatible_deps) < 2:
            return None

        # Find the state index where each conflicting identifier was pinned
        levels: dict[KT, int] = {}
        for idx, state in enumerate(self._states):
            for ident in incompatible_deps:
                if ident not in levels and ident in state.mapping:
                    levels[ident] = idx

        if len(levels) < 2:
            return None

        sorted_levels = sorted(levels.values(), reverse=True)
        return sorted_levels[1]

    def _save_state(self) -> None:
        """Save states for potential rollback if optimistic backjumping fails."""
        if self._save_states is None:
            self._save_states = [
                State(
                    mapping=s.mapping.copy(),
                    criteria=s.criteria.copy(),
                    backtrack_causes=s.backtrack_causes[:],
                )
                for s in self._states
            ]

    def _rollback_states(self) -> None:
        """Rollback states and disable optimistic backjumping."""
        self._optimistic_backjumping_ratio = 0.0
        if self._save_states:
            self._states = self._save_states
            self._save_states = None

    def _backjump(self, causes: list[RequirementInformation[RT, CT]]) -> bool:
        """Perform backjumping.

        When we enter here, the stack is like this::

            [ state Z ]
            [ state Y ]
            [ state X ]
            .... earlier states are irrelevant.

        1. No pins worked for Z, so it does not have a pin.
        2. We want to reset state Y to unpinned, and pin another candidate.
        3. State X holds what state Y was before the pin, but does not
           have the incompatibility information gathered in state Y.

        Each iteration of the loop will:

        1.  Identify Z. The incompatibility is not always caused by the latest
            state. For example, given three requirements A, B and C, with
            dependencies A1, B1 and C1, where A1 and B1 are incompatible: the
            last state might be related to C, so we want to discard the
            previous state.
        2.  Discard Z.
        3.  Discard Y but remember its incompatibility information gathered
            previously, and the failure we're dealing with right now.
        4.  Push a new state Y' based on X, and apply the incompatibility
            information from Y to Y'.
        5a. If this causes Y' to conflict, we need to backtrack again. Make Y'
            the new Z and go back to step 2.
        5b. If the incompatibilities apply cleanly, end backtracking.
        """
        incompatible_reqs: Iterable[CT | RT] = itertools.chain(
            (c.parent for c in causes if c.parent is not None),
            (c.requirement for c in causes),
        )
        incompatible_deps = {self._p.identify(r) for r in incompatible_reqs}

        # Learn this conflict as a nogood so we can skip candidates that
        # would recreate it, before making expensive provider calls.
        if self._supports_nogood_learning():
            self._learn_conflict(causes)

        # Try a CDCL-style non-chronological backjump first. If the provider
        # supports nogood learning, compute the optimal backjump target and
        # try jumping directly there while collecting incompatibilities from
        # every state we skip. If this fails, fall back to legacy backjumping.
        if self._supports_nogood_learning():
            cdcl_target = self._compute_backjump_target(incompatible_deps)
            if cdcl_target is not None and len(self._states) >= 3:
                # Save state stack in case we need to fall back
                saved_states = [
                    State(
                        mapping=s.mapping.copy(),
                        criteria=s.criteria.copy(),
                        backtrack_causes=s.backtrack_causes[:],
                    )
                    for s in self._states
                ]

                # Remove the trigger state
                del self._states[-1]

                incompatibilities_from_broken: list[tuple[KT, list[CT]]] = []

                # Pop states down to the target, collecting incompatibilities
                while len(self._states) > cdcl_target + 2:
                    skipped = self._states.pop()
                    for k, v in skipped.criteria.items():
                        if v.incompatibilities:
                            incompatibilities_from_broken.append(
                                (k, list(v.incompatibilities))
                            )
                    if skipped.mapping:
                        sk, sc = next(reversed(skipped.mapping.items()))
                        incompatibilities_from_broken.append((sk, [sc]))

                if len(self._states) >= 3:
                    broken_state = self._states.pop()
                    if broken_state.mapping:
                        name, candidate = broken_state.mapping.popitem()

                        for k, v in broken_state.criteria.items():
                            if v.incompatibilities:
                                incompatibilities_from_broken.append(
                                    (k, list(v.incompatibilities))
                                )
                        incompatibilities_from_broken.append((name, [candidate]))

                        self._push_new_state()
                        if self._patch_criteria(incompatibilities_from_broken):
                            return True

                # CDCL attempt failed. Restore state stack and fall through
                # to legacy backjumping which handles this correctly.
                self._states = saved_states

        while len(self._states) >= 3:
            # Remove the state that triggered backtracking.
            del self._states[-1]

            # Original backjumping logic (step-by-step)
            while True:
                # Retrieve the last candidate pin and known incompatibilities.
                try:
                    broken_state = self._states.pop()
                    name, candidate = broken_state.mapping.popitem()
                except (IndexError, KeyError):
                    raise ResolutionImpossible(causes) from None

                if (
                    not self._optimistic_backjumping_ratio
                    and name not in incompatible_deps
                ):
                    break

                if (
                    self._optimistic_backjumping_ratio
                    and self._save_states is None
                    and name not in incompatible_deps
                ):
                    self._save_state()

                current_dependencies = {
                    self._p.identify(d) for d in self._p.get_dependencies(candidate)
                }
                if not current_dependencies.isdisjoint(incompatible_deps):
                    break

                if not broken_state.mapping:
                    break

                if len(self._states) <= 1:
                    raise ResolutionImpossible(causes)

            incompatibilities_from_broken = [
                (k, list(v.incompatibilities)) for k, v in broken_state.criteria.items()
            ]

            # Also mark the newly known incompatibility.
            incompatibilities_from_broken.append((name, [candidate]))

            self._push_new_state()
            success = self._patch_criteria(incompatibilities_from_broken)

            # It works! Let's work on this new state.
            if success:
                return True

            # State does not work after applying known incompatibilities.
            # Try the still previous state.

        # No way to backtrack anymore.
        return False

    def _extract_causes(
        self, criteria: list[Criterion[RT, CT]]
    ) -> list[RequirementInformation[RT, CT]]:
        """Extract causes from list of criteria and deduplicate"""
        return list({id(i): i for c in criteria for i in c.information}.values())

    def resolve(self, requirements: Iterable[RT], max_rounds: int) -> State[RT, CT, KT]:
        if self._states:
            raise RuntimeError("already resolved")

        self._r.starting()

        # Initialize the root state.
        self._states = [
            State(
                mapping=collections.OrderedDict(),
                criteria={},
                backtrack_causes=[],
            )
        ]
        for r in requirements:
            try:
                self._add_to_criteria(self.state.criteria, r, parent=None)
            except RequirementsConflicted as e:
                raise ResolutionImpossible(e.criterion.information) from e

        # The root state is saved as a sentinel so the first ever pin can have
        # something to backtrack to if it fails. The root state is basically
        # pinning the virtual "root" package in the graph.
        self._push_new_state()

        # Variables for optimistic backjumping
        optimistic_rounds_cutoff: int | None = None
        optimistic_backjumping_start_round: int | None = None

        for round_index in range(max_rounds):
            self._r.starting_round(index=round_index)

            # Handle if optimistic backjumping has been running for too long
            if self._optimistic_backjumping_ratio and self._save_states is not None:
                if optimistic_backjumping_start_round is None:
                    optimistic_backjumping_start_round = round_index
                    optimistic_rounds_cutoff = int(
                        (max_rounds - round_index) * self._optimistic_backjumping_ratio
                    )

                    if optimistic_rounds_cutoff <= 0:
                        self._rollback_states()
                        continue
                elif optimistic_rounds_cutoff is not None:
                    if (
                        round_index - optimistic_backjumping_start_round
                        >= optimistic_rounds_cutoff
                    ):
                        self._rollback_states()
                        continue

            unsatisfied_names = [
                key
                for key, criterion in self.state.criteria.items()
                if not self._is_current_pin_satisfying(key, criterion)
            ]

            # All criteria are accounted for. Nothing more to pin, we are done!
            if not unsatisfied_names:
                self._r.ending(state=self.state)
                return self.state

            # keep track of satisfied names to calculate diff after pinning
            satisfied_names = set(self.state.criteria.keys()) - set(unsatisfied_names)

            if len(unsatisfied_names) > 1:
                narrowed_unstatisfied_names = list(
                    self._p.narrow_requirement_selection(
                        identifiers=unsatisfied_names,
                        resolutions=self.state.mapping,
                        candidates=IteratorMapping(
                            self.state.criteria,
                            operator.attrgetter("candidates"),
                        ),
                        information=IteratorMapping(
                            self.state.criteria,
                            operator.attrgetter("information"),
                        ),
                        backtrack_causes=self.state.backtrack_causes,
                    )
                )
            else:
                narrowed_unstatisfied_names = unsatisfied_names

            # If there are no unsatisfied names use unsatisfied names
            if not narrowed_unstatisfied_names:
                raise RuntimeError("narrow_requirement_selection returned 0 names")

            # If there is only 1 unsatisfied name skip calling self._get_preference
            if len(narrowed_unstatisfied_names) > 1:
                # Choose the most preferred unpinned criterion to try.
                name = min(narrowed_unstatisfied_names, key=self._get_preference)
            else:
                name = narrowed_unstatisfied_names[0]

            failure_criterion = self._attempt_to_pin_criterion(name)

            if failure_criterion:
                causes = self._extract_causes(failure_criterion)
                # Backjump if pinning fails. The backjump process puts us in
                # an unpinned state, so we can work on it in the next round.
                self._r.resolving_conflicts(causes=causes)

                success = False  # Default; will be set by _backjump if no exception
                try:
                    success = self._backjump(causes)
                except ResolutionImpossible:
                    if self._optimistic_backjumping_ratio and self._save_states:
                        failed_optimistic_backjumping = True
                    else:
                        raise
                else:
                    failed_optimistic_backjumping = bool(
                        not success
                        and self._optimistic_backjumping_ratio
                        and self._save_states
                    )

                if failed_optimistic_backjumping and self._save_states:
                    self._rollback_states()
                else:
                    self.state.backtrack_causes[:] = causes

                    # Dead ends everywhere. Give up.
                    if not success:
                        raise ResolutionImpossible(self.state.backtrack_causes)
            else:
                # discard as information sources any invalidated names
                # (unsatisfied names that were previously satisfied)
                newly_unsatisfied_names = {
                    key
                    for key, criterion in self.state.criteria.items()
                    if key in satisfied_names
                    and not self._is_current_pin_satisfying(key, criterion)
                }
                self._remove_information_from_criteria(
                    self.state.criteria, newly_unsatisfied_names
                )
                # Pinning was successful. Push a new state to do another pin.
                self._push_new_state()

            self._r.ending_round(index=round_index, state=self.state)

        raise ResolutionTooDeep(max_rounds)


class Resolver(AbstractResolver[RT, CT, KT]):
    """The thing that performs the actual resolution work."""

    base_exception = ResolverException

    def resolve(  # type: ignore[override]
        self,
        requirements: Iterable[RT],
        max_rounds: int = 100,
    ) -> Result[RT, CT, KT]:
        """Take a collection of constraints, spit out the resolution result.

        The return value is a representation to the final resolution result. It
        is a tuple subclass with three public members:

        * `mapping`: A dict of resolved candidates. Each key is an identifier
            of a requirement (as returned by the provider's `identify` method),
            and the value is the resolved candidate.
        * `graph`: A `DirectedGraph` instance representing the dependency tree.
            The vertices are keys of `mapping`, and each edge represents *why*
            a particular package is included. A special vertex `None` is
            included to represent parents of user-supplied requirements.
        * `criteria`: A dict of "criteria" that hold detailed information on
            how edges in the graph are derived. Each key is an identifier of a
            requirement, and the value is a `Criterion` instance.

        The following exceptions may be raised if a resolution cannot be found:

        * `ResolutionImpossible`: A resolution cannot be found for the given
            combination of requirements. The `causes` attribute of the
            exception is a list of (requirement, parent), giving the
            requirements that could not be satisfied.
        * `ResolutionTooDeep`: The dependency tree is too deeply nested and
            the resolver gave up. This is usually caused by a circular
            dependency, but you can try to resolve this by increasing the
            `max_rounds` argument.
        """
        resolution = Resolution(self.provider, self.reporter)
        state = resolution.resolve(requirements, max_rounds=max_rounds)
        return _build_result(state)


def _has_route_to_root(
    criteria: Mapping[KT, Criterion[RT, CT]],
    key: KT | None,
    all_keys: dict[int, KT | None],
    connected: set[KT | None],
) -> bool:
    if key in connected:
        return True
    if key not in criteria:
        return False
    assert key is not None
    for p in criteria[key].iter_parent():
        try:
            pkey = all_keys[id(p)]
        except KeyError:
            continue
        if pkey in connected:
            connected.add(key)
            return True
        if _has_route_to_root(criteria, pkey, all_keys, connected):
            connected.add(key)
            return True
    return False
