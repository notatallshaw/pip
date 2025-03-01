import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, Iterable, Optional, Sequence

import pytest

from pip._vendor.resolvelib.resolvers import RequirementInformation

from pip._internal.req.constructors import install_req_from_req_string
from pip._internal.resolution.resolvelib.base import Candidate
from pip._internal.resolution.resolvelib.candidates import REQUIRES_PYTHON_IDENTIFIER
from pip._internal.resolution.resolvelib.factory import Factory
from pip._internal.resolution.resolvelib.provider import PipProvider
from pip._internal.resolution.resolvelib.requirements import (
    ExplicitRequirement,
    SpecifierRequirement,
)

if TYPE_CHECKING:
    from pip._vendor.resolvelib.providers import Preference

    from pip._internal.resolution.resolvelib.base import Candidate, Requirement

    PreferenceInformation = RequirementInformation[Requirement, Candidate]


class FakeCandidate(Candidate):
    """A minimal fake candidate for testing purposes."""

    def __init__(self) -> None: ...


@dataclass
class PreferenceInformationBuilder:
    """
    Helper class to build PreferenceInformation instances.
    """

    name: str
    requirement: Optional[str] = None
    is_direct: bool = False
    parent: Optional[Candidate] = None

    def build(self) -> "PreferenceInformation":
        if self.requirement is None:
            raise ValueError("Requirement must be set to build a PreferenceInformation")

        install_requirement = install_req_from_req_string(self.requirement)
        requirement_information: PreferenceInformation = RequirementInformation(
            requirement=SpecifierRequirement(install_requirement),
            parent=self.parent,
        )
        return requirement_information

    def build_direct(self) -> "PreferenceInformation":
        """Build a direct requirement using a minimal FakeCandidate."""
        direct_requirement = ExplicitRequirement(FakeCandidate())
        return RequirementInformation(
            requirement=direct_requirement, parent=self.parent
        )

    def __call__(self) -> "PreferenceInformation":
        if self.is_direct:
            return self.build_direct()

        return self.build()


@pytest.mark.parametrize(
    "identifier, pref_info_builders, backtrack_builders, user_requested, expected",
    [
        # Test case for REQUIRES_PYTHON_IDENTIFIER
        (
            REQUIRES_PYTHON_IDENTIFIER,
            [PreferenceInformationBuilder(REQUIRES_PYTHON_IDENTIFIER, "python")],
            [],
            {},
            (False, True, True, True, math.inf, True, REQUIRES_PYTHON_IDENTIFIER),
        ),
        # Pinned package with "=="
        (
            "pinned-package",
            [PreferenceInformationBuilder("pinned-package", "pinned-package==1.0")],
            [],
            {},
            (True, True, False, True, math.inf, False, "pinned-package"),
        ),
        # Star-specified package, i.e. with "*"
        (
            "star-specified-package",
            [
                PreferenceInformationBuilder(
                    "star-specified-package", "star-specified-package==1.*"
                )
            ],
            [],
            {},
            (True, True, True, True, math.inf, False, "star-specified-package"),
        ),
        # Package that caused backtracking
        (
            "backtrack-package",
            [PreferenceInformationBuilder("backtrack-package", "backtrack-package")],
            [PreferenceInformationBuilder("backtrack-package", "backtrack-package")],
            {},
            (True, True, True, False, math.inf, True, "backtrack-package"),
        ),
        # Root package requested by user
        (
            "root-package",
            [PreferenceInformationBuilder("root-package", "root-package")],
            [],
            {"root-package": 1},
            (True, True, True, True, 1, True, "root-package"),
        ),
        # Unfree package (with specifier operator)
        (
            "unfree-package",
            [PreferenceInformationBuilder("unfree-package", "unfree-package<1")],
            [],
            {},
            (True, True, True, True, math.inf, False, "unfree-package"),
        ),
        # Free package (no operator)
        (
            "free-package",
            [PreferenceInformationBuilder("free-package", "free-package")],
            [],
            {},
            (True, True, True, True, math.inf, True, "free-package"),
        ),
        # Test case for "direct" preference (explicit URL)
        (
            "direct-package",
            [
                PreferenceInformationBuilder(
                    "direct-package", "direct-package", is_direct=True
                )
            ],
            [],
            {},
            (True, False, True, True, math.inf, True, "direct-package"),
        ),
    ],
)
def test_get_preference(
    identifier: str,
    pref_info_builders: Sequence[PreferenceInformationBuilder],
    backtrack_builders: Sequence[PreferenceInformationBuilder],
    user_requested: Dict[str, int],
    expected: "Preference",
    factory: Factory,
) -> None:
    provider = PipProvider(
        factory=factory,
        constraints={},
        ignore_dependencies=False,
        upgrade_strategy="to-satisfy-only",
        user_requested=user_requested,
    )

    preference_information_map: dict[str, Iterable[PreferenceInformation]] = {}
    for preference_info_builder in pref_info_builders:
        preference_information_map[preference_info_builder.name] = [
            preference_info_builder()
        ]

    backtrack_causes_information: list[PreferenceInformation] = [
        bb() for bb in backtrack_builders
    ]

    preference = provider.get_preference(
        identifier, {}, {}, preference_information_map, backtrack_causes_information
    )

    assert preference == expected, f"Expected {expected}, got {preference}"
