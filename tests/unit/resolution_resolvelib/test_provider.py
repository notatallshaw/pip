from typing import TYPE_CHECKING, Dict, Iterable, Optional, Sequence

import pytest

from pip._vendor.resolvelib.resolvers import RequirementInformation

from pip._internal.req.constructors import install_req_from_req_string
from pip._internal.resolution.resolvelib.base import Candidate, Requirement
from pip._internal.resolution.resolvelib.candidates import REQUIRES_PYTHON_IDENTIFIER
from pip._internal.resolution.resolvelib.factory import Factory
from pip._internal.resolution.resolvelib.provider import PipProvider
from pip._internal.resolution.resolvelib.requirements import (
    ExplicitRequirement,
    SpecifierRequirement,
)

from tests.lib.index import make_mock_candidate

if TYPE_CHECKING:
    from pip._vendor.resolvelib.providers import Preference

    PreferenceInformation = RequirementInformation[Requirement, Candidate]


def build_specifier_req_info(
    name: str, parent: Optional[Candidate] = None
) -> "PreferenceInformation":
    install_requirement = install_req_from_req_string(name)
    requirement_information: PreferenceInformation = RequirementInformation(
        requirement=SpecifierRequirement(install_requirement),
        parent=parent,
    )
    return requirement_information


def build_explicit_req_info(
    version: str = "1.0.0",
    parent: Optional[Candidate] = None,
) -> "PreferenceInformation":
    # This makes a mock InstallationCandidate from pip internal models
    # But what we actually need is a mock candidate from pip resolvelib candidate
    candidate = make_mock_candidate(version)

    requirement_information: PreferenceInformation = RequirementInformation(
        requirement=ExplicitRequirement(candidate), # type: ignore
        parent=parent,
    )

    return requirement_information


@pytest.mark.parametrize(
    "identifier, information, backtrack_causes, user_requested, expected",
    [
        # Test case for REQUIRES_PYTHON_IDENTIFIER
        (
            REQUIRES_PYTHON_IDENTIFIER,
            {REQUIRES_PYTHON_IDENTIFIER: [build_specifier_req_info("python")]},
            [],
            {REQUIRES_PYTHON_IDENTIFIER: 1},
            (False, True, True, True, 1, True, REQUIRES_PYTHON_IDENTIFIER),
        ),
        # Direct requirement
        (
            "direct-package",
            {"direct-package": [build_explicit_req_info("1.0.0")]},
            [],
            {"direct-package": 1},
            (True, False, True, True, 1, True, 'direct-package'),
        ),
        # Pinned package with "=="
        (
            "pinned-package",
            {"pinned-package": [build_specifier_req_info("pinned-package==1.0")]},
            [],
            {"pinned-package": 1},
            (True, True, False, True, 1, False, 'pinned-package'),
        ),
        # Not pinned package with "==1.*"
        (
            "not-pinned-package",
            {"not-pinned-package": [build_specifier_req_info("not-pinned-package==1.*")]},
            [],
            {"not-pinned-package": 1},
            (True, True, True, True, 1, False, 'not-pinned-package'),
        ),
        # Package that caused backtracking
        (
            "backtrack-package",
            {"backtrack-package": [build_specifier_req_info("backtrack-package")]},
            [build_specifier_req_info("backtrack-package")],
            {"backtrack-package": 1},
            (True, True, True, False, 1, True, 'backtrack-package'),
        ),
        # Unfree package (with specifier operator)
        (
            "unfree-package",
            {"unfree-package": [build_specifier_req_info("unfree-package>1")]},
            [],
            {"unfree-package": 1},
            (True, True, True, True, 1, False, 'unfree-package'),
        ),
        # Free package (no operator)
        (
            "free-package",
            {"free-package": [build_specifier_req_info("free-package")]},
            [],
            {"free-package": 1},
            (True, True, True, True, 1, True, 'free-package'),
        ),
    ],
)
def test_get_preference(
    identifier: str,
    information: Dict[str, Iterable["PreferenceInformation"]],
    backtrack_causes: Sequence["PreferenceInformation"],
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

    preference = provider.get_preference(
        identifier, {}, {}, information, backtrack_causes
    )

    assert preference == expected, f"Expected {expected}, got {preference}"
