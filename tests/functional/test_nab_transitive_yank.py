from tests.lib import PipTestEnvironment, create_basic_wheel_for_package


def test_exact_transitive_pin_can_select_a_yanked_release(
    script: PipTestEnvironment,
) -> None:
    files = script.scratch_path / "files"
    files.mkdir()
    app = create_basic_wheel_for_package(script, "app", "1.0", depends=["dep==1.0"])
    dep = create_basic_wheel_for_package(script, "dep", "1.0")
    app = app.rename(files / app.name)
    dep = dep.rename(files / dep.name)
    listing = script.scratch_path / "links.html"
    listing.write_text(
        f'<a href="files/{app.name}">{app.name}</a>\n'
        f'<a href="files/{dep.name}" data-yanked="withdrawn">{dep.name}</a>\n'
    )

    result = script.pip(
        "install",
        "--no-index",
        "--find-links",
        listing,
        "app",
        allow_stderr_warning=True,
    )

    script.assert_installed(app="1.0", dep="1.0")
    assert "yanked version" in result.stderr
