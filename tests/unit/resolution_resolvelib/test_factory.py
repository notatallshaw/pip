from pip._vendor.packaging.specifiers import SpecifierSet

from pip._internal.resolution.resolvelib.base import Constraint
from pip._internal.resolution.resolvelib.candidates import REQUIRES_PYTHON_IDENTIFIER
from pip._internal.resolution.resolvelib.factory import Factory
from pip._internal.resolution.resolvelib.provider import PipProvider


def test_find_candidates_for_requires_python(factory: Factory) -> None:
    requirement = factory.make_requires_python_requirement(SpecifierSet(">=3.0"))
    assert requirement is not None
    assert requirement.name == REQUIRES_PYTHON_IDENTIFIER

    candidates = list(
        factory.find_candidates(
            identifier=REQUIRES_PYTHON_IDENTIFIER,
            requirements={REQUIRES_PYTHON_IDENTIFIER: [requirement]},
            incompatibilities={},
            constraint=Constraint.empty(),
            prefers_installed=False,
            is_satisfied_by=PipProvider.is_satisfied_by,
        )
    )

    assert [candidate.name for candidate in candidates] == [REQUIRES_PYTHON_IDENTIFIER]
