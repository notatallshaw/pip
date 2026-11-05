from __future__ import annotations

import logging
from pathlib import Path

import pytest

from pip._internal.metadata import get_environment
from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.nab.candidates import AlreadyInstalledCandidate
from pip._internal.resolution.nab.factory import Factory
from pip._internal.resolution.nab.reporter import PipReporter


@pytest.fixture
def candidate(tmp_path: Path, factory: Factory) -> AlreadyInstalledCandidate:
    """Supply a real installed distribution to the rejection reporter."""
    metadata = tmp_path / "django-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: django\nVersion: 1.0\n"
    )

    distribution = get_environment([str(tmp_path)]).get_distribution("django")
    assert distribution is not None
    return AlreadyInstalledCandidate(
        distribution, install_req_from_line("django"), factory
    )


def test_no_multiple_versions_message_on_first_rejection(
    caplog: pytest.LogCaptureFixture, candidate: AlreadyInstalledCandidate
) -> None:
    caplog.set_level(logging.INFO)
    reporter = PipReporter()
    reporter.rejecting_candidate((), candidate)
    assert "looking at multiple versions" not in caplog.text


def test_multiple_versions_message_on_second_rejection(
    caplog: pytest.LogCaptureFixture, candidate: AlreadyInstalledCandidate
) -> None:
    caplog.set_level(logging.INFO)
    reporter = PipReporter()
    reporter.rejecting_candidate((), candidate)
    reporter.rejecting_candidate((), candidate)
    assert "looking at multiple versions of django" in caplog.text
