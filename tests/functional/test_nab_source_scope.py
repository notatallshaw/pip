from textwrap import dedent

from tests.lib import PipTestEnvironment, create_basic_wheel_for_package
from tests.lib.wheel import make_wheel


def test_url_from_rejected_parent_does_not_replace_installed_candidate(
    script: PipTestEnvironment,
) -> None:
    wheel = create_basic_wheel_for_package(script, "dep", "1.0")
    script.pip("install", "--no-index", wheel)
    source = script.scratch_path / "direct"
    source.mkdir()
    replacement = create_basic_wheel_for_package(
        script, "dep", "1.0", depends=["unavailable==1.0"]
    )
    replacement = replacement.rename(source / replacement.name)
    create_basic_wheel_for_package(script, "app", "1.0", depends=["dep==1.0"])
    make_wheel(
        name="app",
        version="2.0",
        metadata=dedent(f"""\
            Metadata-Version: 2.1
            Name: app
            Version: 2.0
            Requires-Dist: dep @ {replacement.as_uri()}
            """),
    ).save_to(script.scratch_path / "app-2.0-py2.py3-none-any.whl")

    script.pip(
        "install", "--no-index", "--find-links", script.scratch_path, "dep", "app"
    )

    script.assert_installed(app="1.0", dep="1.0")


def test_urls_from_alternative_parent_versions_are_independent(
    script: PipTestEnvironment,
) -> None:
    sources = []
    for name, dependencies in (("refused", ["unavailable==1.0"]), ("usable", [])):
        source = script.scratch_path / name
        source.mkdir()
        wheel = create_basic_wheel_for_package(
            script, "dep", "1.0", depends=dependencies
        )
        sources.append(wheel.rename(source / wheel.name))
    for version, wheel in zip(("2.0", "1.0"), sources):
        make_wheel(
            name="app",
            version=version,
            metadata=dedent(f"""\
                Metadata-Version: 2.1
                Name: app
                Version: {version}
                Requires-Dist: dep @ {wheel.as_uri()}
                """),
        ).save_to(script.scratch_path / f"app-{version}-py2.py3-none-any.whl")

    script.pip("install", "--no-index", "--find-links", script.scratch_path, "app")

    script.assert_installed(app="1.0", dep="1.0")
