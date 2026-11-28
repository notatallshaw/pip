import contextlib
import gc
from collections.abc import Iterator

import pytest

from pip._internal.index.package_finder import PackageFinder
from pip._internal.operations.prepare import RequirementPreparer
from pip._internal.req.constructors import install_req_from_line
from pip._internal.resolution.nab import resolver as resolver_module


@pytest.mark.parametrize("cpython", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_collector_state_survives_success_and_failure(
    monkeypatch: pytest.MonkeyPatch, cpython: bool, enabled: bool, failure: bool
) -> None:
    original = gc.isenabled()
    monkeypatch.setattr(resolver_module, "_IS_CPYTHON", cpython)
    (gc.enable if enabled else gc.disable)()
    expected = pytest.raises(RuntimeError) if failure else contextlib.nullcontext()
    try:
        with expected, resolver_module._collector_paused():
            assert gc.isenabled() == (enabled and not cpython)
            if failure:
                raise RuntimeError("metadata failure")
        assert gc.isenabled() == enabled
    finally:
        (gc.enable if original else gc.disable)()


@pytest.mark.parametrize("failure", [False, True])
def test_real_resolution_restores_collection_after_its_factory_context(
    preparer: RequirementPreparer,
    finder: PackageFinder,
    monkeypatch: pytest.MonkeyPatch,
    failure: bool,
) -> None:
    resolver = resolver_module.Resolver(
        preparer=preparer,
        finder=finder,
        wheel_cache=None,
        make_install_req=install_req_from_line,
        use_user_site=False,
        ignore_dependencies=False,
        only_dependencies=False,
        ignore_installed=True,
        ignore_requires_python=False,
        force_reinstall=False,
        upgrade_strategy="to-satisfy-only",
    )
    owner = resolver.factory
    inputs = owner.collect_root_requirements([])
    factory_context = owner.catalogue_context
    observed = []

    @contextlib.contextmanager
    def context() -> Iterator[None]:
        observed.append(gc.isenabled())
        with factory_context():
            if failure:
                raise RuntimeError("factory failure")
            yield

    original = gc.isenabled()
    monkeypatch.setattr(resolver_module, "_IS_CPYTHON", True)
    monkeypatch.setattr(owner, "catalogue_context", context)
    expected = pytest.raises(RuntimeError) if failure else contextlib.nullcontext()
    gc.enable()
    try:
        with expected:
            resolver._resolve(inputs)
        assert observed == [False]
        assert gc.isenabled()
    finally:
        (gc.enable if original else gc.disable)()
