"""Run pip install and record file preparation and dependency metadata reads."""

import json
import sys
from collections.abc import Iterable, Iterator
from unittest.mock import patch

from pip._internal.cli.main import main
from pip._internal.metadata import BaseDistribution
from pip._internal.req.req_install import InstallRequirement
from pip._internal.resolution.nab.base import Candidate, Requirement
from pip._internal.resolution.nab.candidates import LinkCandidate
from pip._internal.resolution.nab.factory import Factory

prepare_file = LinkCandidate._prepare
read_requirements = Factory.make_requirements_from_spec


def prepare(candidate: LinkCandidate) -> BaseDistribution:
    link = candidate.source_link
    assert link is not None
    print("PREPARE " + link.filename, flush=True)
    return prepare_file(candidate)


def requirements(
    factory: Factory,
    specifier: str,
    comes_from: InstallRequirement | None,
    requested_extras: Iterable[str] = (),
    *,
    parent: Candidate | None = None,
) -> Iterator[Requirement]:
    print("DEPENDENCY_READ " + json.dumps(specifier), flush=True)
    return read_requirements(
        factory, specifier, comes_from, requested_extras, parent=parent
    )


if __name__ == "__main__":
    with (
        patch.object(LinkCandidate, "_prepare", prepare),
        patch.object(Factory, "make_requirements_from_spec", requirements),
    ):
        sys.exit(main())
