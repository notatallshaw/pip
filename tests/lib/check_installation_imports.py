"""Check dynamic imports against the real installation audit hook."""

import sys
import warnings
from collections.abc import Callable

from pip._vendor.pygments.plugin import (
    find_plugin_filters,
    find_plugin_formatters,
    find_plugin_lexers,
    find_plugin_styles,
)

from pip._internal.commands import create_command
from pip._internal.commands.debug import get_module_from_module_name
from pip._internal.commands.install import _prevent_further_imports
from pip._internal.utils.compat import open_text_resource
from pip._internal.utils.deprecation import PipDeprecationWarning


def main() -> None:
    kind = sys.argv[1]
    imports: dict[str, tuple[str, Callable[[], object]]] = {
        "command": (
            "pip._internal.commands.hash",
            lambda: create_command("hash").name,
        ),
        "vendor": (
            "pip._vendor.distlib",
            lambda: get_module_from_module_name("distlib").__name__,
        ),
        "resource": ("guarded_import_resource", read_resource),
        "lexer": ("guarded_import_plugin", lambda: list(find_plugin_lexers())),
        "formatter": ("guarded_import_plugin", lambda: list(find_plugin_formatters())),
        "style": ("guarded_import_plugin", lambda: list(find_plugin_styles())),
        "filter": ("guarded_import_plugin", lambda: list(find_plugin_filters())),
    }
    module_name, load = imports[kind]
    assert module_name not in sys.modules

    _prevent_further_imports()
    with warnings.catch_warnings():
        warnings.simplefilter("error", PipDeprecationWarning)
        try:
            load()
        except PipDeprecationWarning:
            pass
        else:
            raise AssertionError(f"Imported {module_name} during installation")

    assert module_name not in sys.modules
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PipDeprecationWarning)
        loaded = load()
    assert module_name in sys.modules

    # Cached imports do not trigger an unexpected-import warning.
    with warnings.catch_warnings():
        warnings.simplefilter("error", PipDeprecationWarning)
        assert load() == loaded

    if kind == "command":
        assert loaded == "hash"
    elif kind == "vendor":
        assert loaded == module_name
    elif kind == "resource":
        assert loaded == "resource contents"
    elif kind == "lexer":
        assert loaded == [42]
    else:
        assert loaded == [("guarded", 42)]


def read_resource() -> str:
    with open_text_resource("guarded_import_resource", "resource.txt") as stream:
        return stream.read()


if __name__ == "__main__":
    main()
