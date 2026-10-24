import errno
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest

from pip._vendor.requests.exceptions import InvalidProxyURL

import pip
from pip._internal.commands import install
from pip._internal.commands.install import (
    _prevent_further_imports,
    create_os_error_message,
    decide_user_install,
)


class TestDecideUserInstall:
    @mock.patch("site.ENABLE_USER_SITE", True)
    @mock.patch("pip._internal.commands.install.site_packages_writable")
    def test_prefix_and_target(self, sp_writable: mock.Mock) -> None:
        sp_writable.return_value = False

        assert decide_user_install(use_user_site=None, prefix_path="foo") is False

        assert decide_user_install(use_user_site=None, target_dir="bar") is False

    @pytest.mark.parametrize(
        "enable_user_site,site_packages_writable,result",
        [
            (True, True, False),
            (True, False, True),
            (False, True, False),
            (False, False, False),
        ],
    )
    def test_most_cases(
        self,
        enable_user_site: bool,
        site_packages_writable: bool,
        result: bool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("site.ENABLE_USER_SITE", enable_user_site)
        monkeypatch.setattr(
            "pip._internal.commands.install.site_packages_writable",
            lambda **kw: site_packages_writable,
        )
        assert decide_user_install(use_user_site=None) is result


@pytest.mark.parametrize(
    "error, show_traceback, using_user_site, expected",
    [
        # show_traceback = True, using_user_site = True
        (
            OSError("Illegal byte sequence"),
            True,
            True,
            "Could not install packages due to an OSError.\n",
        ),
        (
            OSError(errno.EACCES, "No file permission"),
            True,
            True,
            "Could"
            " not install packages due to an OSError.\nCheck the"
            " permissions.\n",
        ),
        # show_traceback = True, using_user_site = False
        (
            OSError("Illegal byte sequence"),
            True,
            False,
            "Could not install packages due to an OSError.\n",
        ),
        (
            OSError(errno.EACCES, "No file permission"),
            True,
            False,
            "Could"
            " not install packages due to an OSError.\nConsider using the"
            " `--user` option or check the permissions.\n",
        ),
        # show_traceback = False, using_user_site = True
        (
            OSError("Illegal byte sequence"),
            False,
            True,
            "Could not"
            " install packages due to an OSError: Illegal byte"
            " sequence\n",
        ),
        (
            OSError(errno.EACCES, "No file permission"),
            False,
            True,
            "Could"
            " not install packages due to an OSError: [Errno 13] No file"
            " permission\nCheck the permissions.\n",
        ),
        # show_traceback = False, using_user_site = False
        (
            OSError("Illegal byte sequence"),
            False,
            False,
            "Could not"
            " install packages due to an OSError: Illegal byte sequence"
            "\n",
        ),
        (
            OSError(errno.EACCES, "No file permission"),
            False,
            False,
            "Could not install packages due to an OSError: [Errno 13] No"
            " file permission\nConsider using the `--user` option or check the"
            " permissions.\n",
        ),
        # Testing custom InvalidProxyURL with help message
        #  show_traceback = True, using_user_site = True
        (
            InvalidProxyURL(),
            True,
            True,
            "Could not install packages due to an OSError.\n"
            "Consider checking your local proxy configuration"
            ' with "pip config debug".\n',
        ),
        # Testing both long path error (ENOENT)
        # and long file/folder name error (EINVAL) on Windows
        pytest.param(
            OSError(errno.ENOENT, "No such file or directory", f"C:{'/a'*261}"),
            False,
            False,
            "Could not install packages due to an OSError: "
            f"[Errno 2] No such file or directory: 'C:{'/a'*261}'\n"
            "HINT: This error might have occurred since "
            "this system does not have Windows Long Path "
            "support enabled. You can find information on "
            "how to enable this at "
            "https://pip.pypa.io/warnings/enable-long-paths\n",
            marks=pytest.mark.skipif(
                sys.platform != "win32", reason="Windows-specific filename length test"
            ),
        ),
        pytest.param(
            OSError(errno.EINVAL, "No such file or directory", f"C:/{'a'*256}"),
            False,
            False,
            "Could not install packages due to an OSError: "
            f"[Errno 22] No such file or directory: 'C:/{'a'*256}'\n"
            "HINT: This error might be caused by a file or folder name exceeding "
            "255 characters, which is a Windows limitation even if long paths "
            "are enabled.\n",
            marks=pytest.mark.skipif(
                sys.platform != "win32", reason="Windows-specific filename length test"
            ),
        ),
        pytest.param(
            OSError(
                errno.EINVAL, "No such file or directory", f"C:{'/a'*261}/{'b'*256}"
            ),
            False,
            False,
            "Could not install packages due to an OSError: "
            f"[Errno 22] No such file or directory: 'C:{'/a' * 261}/{'b' * 256}'\n"
            "HINT: This error might be caused by a file or folder name exceeding "
            "255 characters, which is a Windows limitation even if long paths "
            "are enabled.\n "
            "HINT: This error might have occurred since "
            "this system does not have Windows Long Path "
            "support enabled. You can find information on "
            "how to enable this at "
            "https://pip.pypa.io/warnings/enable-long-paths\n",
            marks=pytest.mark.skipif(
                sys.platform != "win32", reason="Windows-specific filename length test"
            ),
        ),
    ],
)
def test_create_os_error_message(
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
    show_traceback: bool,
    using_user_site: bool,
    expected: str,
) -> None:
    monkeypatch.setattr(install, "running_under_virtualenv", lambda: False)
    msg = create_os_error_message(error, show_traceback, using_user_site)
    assert msg == expected


def test_prevent_further_imports_blocks_import(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(install, "_IMPORT_AUDIT_HOOK_INSTALLED", False)
    monkeypatch.setattr(install, "_ACTIVE_INSTALLATIONS", set())
    monkeypatch.setattr(install, "_WARNED_IMPORTS", set())
    monkeypatch.setattr(pip, "_EAGER_IMPORTS", {"unknown_module"})
    add_hook = mock.Mock()
    monkeypatch.setattr(sys, "addaudithook", add_hook)
    with _prevent_further_imports():
        with _prevent_further_imports():
            pass
        for module in ("unknown_module", "os", "encodings.iso8859_15"):
            with pytest.raises(install.BlockedImportError, match=module):
                install._prevent_import_hook("import", (module,))
        install._prevent_import_hook("open", ("unknown_module",))

    assert not install._ACTIVE_INSTALLATIONS
    for _ in range(2):
        install._prevent_import_hook("import", ("after_install",))
    assert len(caplog.records) == 1
    assert "Unsafe import" in caplog.text
    assert "all required modules before running pip install" in caplog.text
    with _prevent_further_imports():
        with pytest.raises(install.BlockedImportError, match="after_install"):
            install._prevent_import_hook("import", ("after_install",))
    add_hook.assert_called_once_with(install._prevent_import_hook)


def test_import_guard_restores_warning_mode_after_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(install, "_IMPORT_AUDIT_HOOK_INSTALLED", False)
    monkeypatch.setattr(install, "_ACTIVE_INSTALLATIONS", set())
    monkeypatch.setattr(install, "_WARNED_IMPORTS", set())
    with mock.patch.object(sys, "addaudithook"):
        with pytest.raises(RuntimeError):
            with _prevent_further_imports():
                raise RuntimeError("installation failed")

    assert not install._ACTIVE_INSTALLATIONS
    install._prevent_import_hook("import", ("after_failure",))
    assert "Unsafe import" in caplog.text


def test_post_install_warning_allows_logging_imports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(install, "_ACTIVE_INSTALLATIONS", set())
    monkeypatch.setattr(install, "_WARNED_IMPORTS", set())

    def warning(*args: object) -> None:
        install._prevent_import_hook("import", ("handler_module",))
        raise RuntimeError("The caller's logging handler failed")

    with mock.patch.object(install.logger, "warning", side_effect=warning) as log:
        install._prevent_import_hook("import", ("handler_module",))
    log.assert_called_once()


@pytest.mark.parametrize(
    "error",
    [
        ImportError("Optional backend unavailable"),
        ModuleNotFoundError("Missing dependency", name="backend_dependency"),
    ],
)
def test_eager_import_errors_are_preserved(
    monkeypatch: pytest.MonkeyPatch, error: ImportError
) -> None:
    monkeypatch.setattr(install, "_IMPORT_ERRORS", {"previous_backend": error})
    importer = mock.Mock(side_effect=error)
    monkeypatch.setattr(install, "__import__", importer, raising=False)

    install._eagerly_import_modules(("optional_backend", "optional_backend"))
    assert "previous_backend" not in install._IMPORT_ERRORS
    monkeypatch.setattr(install, "_ACTIVE_INSTALLATIONS", {object()})

    for _ in range(2):
        with pytest.raises(type(error)) as caught:
            install._prevent_import_hook("import", ("optional_backend",))
        assert caught.value is error
    importer.assert_called_once_with("optional_backend")
    monkeypatch.setattr(install, "_ACTIVE_INSTALLATIONS", set())
    install._prevent_import_hook("import", ("optional_backend",))


@pytest.fixture
def lazy_import_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ModuleType]:
    """Isolate installation dependencies with real lazy bindings on Python 3.15+."""
    if sys.version_info < (3, 15):
        pytest.skip("Lazy imports require Python 3.15")

    owner = ModuleType("_pip_reify_owner")
    monkeypatch.setitem(sys.modules, owner.__name__, owner)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(install, "_IMPORT_ERRORS", {})
    try:
        yield owner
    finally:
        for name in tuple(sys.modules):
            if name.startswith("_pip_reify_") and name != owner.__name__:
                del sys.modules[name]
        sys.lazy_modules.difference_update(
            name for name in tuple(sys.lazy_modules) if name.startswith("_pip_reify_")
        )


@pytest.mark.parametrize(
    "statement, preloaded",
    [
        ("lazy import _pip_reify_first as alias", False),
        ("lazy from _pip_reify_first import value as alias", False),
        ("lazy import _pip_reify_first as alias", True),
    ],
)
def test_reify_lazy_imports_loads_transitive_targets(
    lazy_import_owner: ModuleType, tmp_path: Path, statement: str, preloaded: bool
) -> None:
    (tmp_path / "_pip_reify_first.py").write_text(
        "lazy import _pip_reify_second\nvalue = 42\n"
    )
    (tmp_path / "_pip_reify_second.py").write_text("value = 43\n")
    if preloaded:
        __import__("_pip_reify_first")
    exec(statement, lazy_import_owner.__dict__)
    assert ("_pip_reify_first" in sys.modules) == preloaded

    install._reify_lazy_imports((lazy_import_owner.__name__,))

    assert "_pip_reify_first" in sys.modules
    assert "_pip_reify_second" in sys.modules
    assert not install._IMPORT_ERRORS


def test_reify_lazy_imports_revisits_existing_owner(
    lazy_import_owner: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "_pip_reify_first.py").write_text(
        'import sys\nexec("lazy import _pip_reify_second", '
        'sys.modules["_pip_reify_owner"].__dict__)\n'
    )
    (tmp_path / "_pip_reify_second.py").write_text("value = 42\n")
    exec("lazy import _pip_reify_first", lazy_import_owner.__dict__)

    install._reify_lazy_imports((lazy_import_owner.__name__,))

    assert "_pip_reify_second" in sys.modules


def test_reify_lazy_imports_resolves_exported_attributes(
    lazy_import_owner: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "_pip_reify_exports.py"
    source.write_text(
        "def __getattr__(name):\n"
        '    if name == "exported":\n'
        "        import _pip_reify_dependency\n"
        "        return _pip_reify_dependency\n"
        "    raise AttributeError(name)\n"
    )
    (tmp_path / "_pip_reify_dependency.py").write_text("value = 42\n")
    exec(
        "lazy from _pip_reify_exports import exported as alias",
        lazy_import_owner.__dict__,
    )

    install._reify_lazy_imports((lazy_import_owner.__name__,))

    assert "_pip_reify_dependency" in sys.modules
    assert not install._IMPORT_ERRORS


def test_reify_lazy_imports_leaves_unrelated_targets_lazy(
    lazy_import_owner: ModuleType, tmp_path: Path
) -> None:
    unrelated = ModuleType("_pip_reify_unrelated")
    marker = tmp_path / "unrelated-imported"
    (tmp_path / "_pip_reify_unneeded.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        'raise AssertionError("Unrelated lazy import executed")\n'
    )
    exec("lazy import _pip_reify_unneeded", unrelated.__dict__)

    install._reify_lazy_imports((lazy_import_owner.__name__,))

    assert not marker.exists()
    assert "_pip_reify_unneeded" not in sys.modules


def test_reify_lazy_imports_ignores_unavailable_eager_module(
    lazy_import_owner: ModuleType,
) -> None:
    name = "_pip_reify_disabled"
    with mock.patch.dict(sys.modules, {name: None}):
        install._eagerly_import_modules((name,))
        install._reify_lazy_imports((name,))

    assert isinstance(install._IMPORT_ERRORS[name], ModuleNotFoundError)


@pytest.mark.parametrize("exception", ["ImportError", "RuntimeError"])
def test_reify_lazy_imports_attempts_failed_targets_once(
    lazy_import_owner: ModuleType,
    tmp_path: Path,
    exception: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []
    lazy_import_owner.__dict__["attempts"] = attempts
    arguments = (
        "'optional dependency unavailable', name='_pip_reify_failure'"
        if exception == "ImportError"
        else "'optional dependency unavailable'"
    )
    (tmp_path / "_pip_reify_failure.py").write_text(
        "import _pip_reify_owner\n"
        "_pip_reify_owner.attempts.append(1)\n"
        "lazy import _pip_reify_second\n"
        f"raise {exception}({arguments})\n"
    )
    (tmp_path / "_pip_reify_second.py").write_text("value = 42\n")
    exec("lazy import _pip_reify_failure", lazy_import_owner.__dict__)

    install._reify_lazy_imports((lazy_import_owner.__name__,))

    assert attempts == [1]
    assert "_pip_reify_second" not in sys.modules
    assert ("_pip_reify_failure" in install._IMPORT_ERRORS) == (
        exception == "ImportError"
    )
    if exception == "ImportError":
        monkeypatch.setattr(install, "_ACTIVE_INSTALLATIONS", {object()})
        with pytest.raises(ImportError) as cached:
            install._prevent_import_hook("import", ("_pip_reify_failure",))
        assert type(cached.value) is ImportError
        assert str(cached.value) == "optional dependency unavailable"


def test_reify_lazy_imports_preserves_dependency_import_error(
    lazy_import_owner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "_pip_reify_failure"
    source = tmp_path / f"{name}.py"
    source.write_text("import _pip_reify_missing_dependency\n")
    exec(f"lazy import {name} as dependency", lazy_import_owner.__dict__)

    install._reify_lazy_imports((lazy_import_owner.__name__,))

    assert "_pip_reify_missing_dependency" in install._IMPORT_ERRORS
    monkeypatch.setattr(install, "_ACTIVE_INSTALLATIONS", {object()})
    with pytest.raises(ModuleNotFoundError, match="No module named") as missing:
        install._prevent_import_hook("import", ("_pip_reify_missing_dependency",))
    assert missing.value.name == "_pip_reify_missing_dependency"
