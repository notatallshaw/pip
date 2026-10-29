import logging
import os
from optparse import Values

from pip._vendor.packaging.markers import UndefinedComparison, UndefinedEnvironmentName

from pip._internal.cli import cmdoptions
from pip._internal.cli.cmdoptions import make_target_python
from pip._internal.cli.req_command import RequirementCommand, with_cleanup
from pip._internal.cli.status_codes import SUCCESS
from pip._internal.distributions.sdist import SDIST_RESOLUTION_FIELDS
from pip._internal.exceptions import InstallationError
from pip._internal.operations.build.build_tracker import get_build_tracker
from pip._internal.operations.prepare import RequirementPreparer
from pip._internal.req.req_install import (
    InstallRequirement,
    check_invalid_constraint_type,
)
from pip._internal.utils.misc import (
    display_path,
    ensure_dir,
    normalize_path,
    write_output,
)
from pip._internal.utils.temp_dir import TempDirectory

logger = logging.getLogger(__name__)


class DownloadCommand(RequirementCommand):
    """
    Download packages from:

    - PyPI (and other indexes) using requirement specifiers.
    - VCS project urls.
    - Local project directories.
    - Local or remote source archives.

    pip also supports downloading from "requirements files", which provide
    an easy way to specify a whole environment to be downloaded.
    """

    usage = """
      %prog [options] <requirement specifier> [package-index-options] ...
      %prog [options] -r <requirements file> [package-index-options] ...
      %prog [options] <vcs project url> ...
      %prog [options] <local project path> ...
      %prog [options] <archive url/path> ..."""

    def add_options(self) -> None:
        self.cmd_opts.add_option(cmdoptions.constraints())
        self.cmd_opts.add_option(cmdoptions.build_constraints())
        self.cmd_opts.add_option(cmdoptions.requirements())
        self.cmd_opts.add_option(cmdoptions.requirements_from_scripts())
        self.cmd_opts.add_option(cmdoptions.no_deps())
        self.cmd_opts.add_option(cmdoptions.only_deps())
        self.cmd_opts.add_option(cmdoptions.src())
        self.cmd_opts.add_option(cmdoptions.require_hashes())
        self.cmd_opts.add_option(cmdoptions.no_require_hashes())
        self.cmd_opts.add_option(cmdoptions.progress_bar())
        self.cmd_opts.add_option(cmdoptions.no_build_isolation())
        self.cmd_opts.add_option(cmdoptions.use_pep517())
        self.cmd_opts.add_option(cmdoptions.check_build_deps())
        self.cmd_opts.add_option(cmdoptions.ignore_requires_python())

        self.cmd_opts.add_option(
            "-d",
            "--dest",
            "--destination-dir",
            "--destination-directory",
            dest="download_dir",
            metavar="dir",
            default=os.curdir,
            help="Download packages into <dir>.",
        )

        cmdoptions.add_target_python_options(self.cmd_opts)

        index_opts = cmdoptions.make_option_group(
            cmdoptions.index_group,
            self.parser,
        )

        selection_opts = cmdoptions.make_option_group(
            cmdoptions.package_selection_group,
            self.parser,
        )

        self.parser.insert_option_group(0, index_opts)
        self.parser.insert_option_group(0, selection_opts)
        self.parser.insert_option_group(0, self.cmd_opts)

    @staticmethod
    def _single_source_without_metadata(
        reqs: list[InstallRequirement], options: Values
    ) -> InstallRequirement | None:
        """Select a source when no active root needs project metadata."""
        if RequirementCommand.determine_resolver_variant(options) == "legacy":
            return None
        if not options.ignore_dependencies or not options.ignore_requires_python:
            return None
        try:
            active = [req for req in reqs if req.match_markers()]
        except (UndefinedComparison, UndefinedEnvironmentName):
            # Let the resolver preserve its order of root validation and preparation.
            return None
        if len(active) != 1:
            return None
        req = active[0]
        if req.req is not None or req.constraint or req.editable or req.link is None:
            return None
        if req.link.is_wheel or req.link.is_vcs:
            return None
        return req

    def _prepare_anonymous_download(
        self,
        reqs: list[InstallRequirement],
        selected: InstallRequirement,
        preparer: RequirementPreparer,
    ) -> None:
        """Prepare one archive while preserving the order of ignored-root checks."""
        for req in reqs:
            if req is selected:
                preparer.prepare_download_without_metadata(selected)
                continue
            if req.constraint:
                problem = check_invalid_constraint_type(req)
                if problem:
                    raise InstallationError(problem)
            if not req.constraint:
                logger.info(
                    "Ignoring %s: markers '%s' don't match your environment",
                    req.name,
                    req.markers,
                )

    @with_cleanup
    def run(self, options: Values, args: list[str]) -> int:
        options.ignore_installed = True
        # editable doesn't really make sense for `pip download`, but the bowels
        # of the RequirementSet code require that property.
        options.editables = []

        cmdoptions.check_dist_restriction(options)
        cmdoptions.check_build_constraints(options)
        cmdoptions.check_release_control_exclusive(options)
        cmdoptions.check_only_deps_option_does_not_conflict(options)

        options.download_dir = normalize_path(options.download_dir)
        ensure_dir(options.download_dir)

        session = self.get_default_session(options)

        target_python = make_target_python(options)
        finder = self._build_package_finder(
            options=options,
            session=session,
            target_python=target_python,
            ignore_requires_python=options.ignore_requires_python,
        )

        build_tracker = self.enter_context(get_build_tracker())

        directory = TempDirectory(
            delete=not options.no_clean,
            kind="download",
            globally_managed=True,
        )

        reqs = self.get_requirements(args, options, finder, session)

        preparer = self.make_requirement_preparer(
            temp_build_dir=directory,
            options=options,
            build_tracker=build_tracker,
            session=session,
            finder=finder,
            download_dir=options.download_dir,
            use_user_site=False,
            verbosity=self.verbosity,
            allow_editables=False,
            static_metadata_fields=SDIST_RESOLUTION_FIELDS,
        )

        source = self._single_source_without_metadata(reqs, options)
        if source is not None:
            self.trace_basic_info(finder)
            self._prepare_anonymous_download(reqs, source, preparer)
            preparer.save_linked_requirement(source)
            assert source.link is not None
            if source.link.is_existing_dir():
                write_output(
                    "Source directory already available: %s",
                    display_path(source.link.file_path),
                )
            else:
                write_output("Successfully downloaded %s", source.link.filename)
            return SUCCESS

        resolver = self.make_resolver(
            preparer=preparer,
            finder=finder,
            options=options,
            ignore_requires_python=options.ignore_requires_python,
            py_version_info=options.python_version,
        )

        self.trace_basic_info(finder)

        requirement_set = resolver.resolve(reqs, check_supported_wheels=True)

        preparer.prepare_linked_requirements_more(requirement_set.requirements.values())

        downloaded: list[str] = []
        for req in requirement_set.requirements.values():
            if req.satisfied_by is None:
                assert req.name is not None
                preparer.save_linked_requirement(req)
                downloaded.append(req.name)

        if downloaded:
            write_output("Successfully downloaded %s", " ".join(downloaded))

        return SUCCESS
