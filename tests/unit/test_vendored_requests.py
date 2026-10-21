import subprocess
import sys

import pytest

from tests.lib import SRC_DIR


@pytest.mark.parametrize("windows, expected_calls", [(False, 1), (True, 0)])
def test_pyopenssl_fallback_platform(windows: bool, expected_calls: int) -> None:
    code = f"""
import importlib
import ssl
import sys
import types

sys.path.insert(0, {str(SRC_DIR / "src")!r})

from pip._internal.utils import compat

compat.WINDOWS = {windows!r}
ssl.HAS_SNI = False

calls = []
pyopenssl = types.ModuleType("pip._vendor.urllib3.contrib.pyopenssl")
pyopenssl.inject_into_urllib3 = lambda: calls.append(None)
sys.modules[pyopenssl.__name__] = pyopenssl

cryptography = types.ModuleType("cryptography")
cryptography.__version__ = "1.3.4"
sys.modules[cryptography.__name__] = cryptography

importlib.import_module("pip._vendor.requests")

assert len(calls) == {expected_calls}
"""
    subprocess.run([sys.executable, "-I", "-c", code], check=True)
