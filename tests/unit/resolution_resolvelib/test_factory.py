import pytest

from pip._vendor.packaging.specifiers import SpecifierSet

from pip._internal.resolution.resolvelib.factory import specifier_pins_version


@pytest.mark.parametrize(
    "specifier, expected",
    [
        ("", False),
        ("==1.0", True),
        ("===1.0", True),
        ("== 1.0", True),
        ("==1.0+local", True),
        ("==1.0.*", False),
        ("~=1.0", False),
        (">=1.0", False),
        ("!=1.0", False),
        (">=1.0,<2.0", False),
        (">=1.0,==1.5", True),
        ("!=1.5,==1.*", False),
        ("==1.*,===1.5", True),
    ],
)
def test_specifier_pins_version(specifier: str, expected: bool) -> None:
    assert specifier_pins_version(SpecifierSet(specifier)) is expected
