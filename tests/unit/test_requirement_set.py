from pip._internal.req.constructors import install_req_from_line
from pip._internal.req.req_set import RequirementSet
from pip._internal.utils.packaging import get_requirement

from tests.lib import make_wheel


def test_requirements_to_install_tracks_selected_named_objects() -> None:
    requirements = RequirementSet()
    first = install_req_from_line("https://example.invalid/first/package.tar.gz")
    shadowed = install_req_from_line("https://example.invalid/second/package.tar.gz")
    unresolved = install_req_from_line("https://example.invalid/unknown.tar.gz")
    for req in (first, shadowed, unresolved):
        requirements.add_unnamed_requirement(req)
    assert requirements.requirements_to_install == [first, shadowed, unresolved]

    first.req = get_requirement("package==1.0")
    requirements.add_named_requirement(first)
    shadowed.req = get_requirement("package==1.0")
    dependency = install_req_from_line("dependency==1.0")
    requirements.add_named_requirement(dependency)
    constraint = install_req_from_line("constraint==1.0", constraint=True)
    requirements.add_named_requirement(constraint)
    satisfied = install_req_from_line("satisfied==1.0")
    satisfied.satisfied_by = make_wheel("satisfied", "1.0").as_distribution("satisfied")
    requirements.add_named_requirement(satisfied)

    assert requirements.requirements_to_install == [unresolved, first, dependency]

    first.req = get_requirement("renamed-package==1.0")
    requirements.add_named_requirement(first)
    assert requirements.requirements_to_install == [unresolved, first, dependency]

    same_project = install_req_from_line("renamed-package==1.0")
    requirements.add_named_requirement(same_project)
    assert requirements.requirements_to_install == [
        unresolved,
        first,
        dependency,
        same_project,
    ]
