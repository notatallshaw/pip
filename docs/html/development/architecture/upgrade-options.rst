=============================================
Options that control the installation process
=============================================

When installing packages, pip chooses a distribution file, and
installs it in the user's environment. There are many choices (which
are `still evolving`_) involved in deciding which file to install, and
these are controlled by a variety of options.

Controlling what gets installed
===============================

These options directly affect how the resolver uses the list of available
distribution files to decide which one to install. So these modify the
resolution algorithm itself, rather than the input to that algorithm.

``--upgrade``

Prefer newer versions for packages named on the command line or in a
requirements file. A request for ``package[extra]`` also makes the base package
eligible for upgrade. Without ``--upgrade``, pip prefers a suitable installed
version, but can replace it when the request or its dependencies require a
different choice.

``--upgrade-strategy``

This option affects which packages are allowed to be installed. It is only
relevant if ``--upgrade`` is specified (except for the ``to-satisfy-only``
option mentioned below). The base behaviour is to allow
packages specified on pip's command line to be upgraded. This option controls
what *other* packages can be upgraded:

* ``eager`` - prefer newer versions for requested packages and their dependencies.
* ``only-if-needed`` - packages are only upgraded if they are named in the
  pip command or a requirement file (i.e, they are direct requirements), or
  an upgraded parent needs a later version of the dependency than is
  currently installed.
* ``to-satisfy-only`` (**undocumented, please avoid**) - packages are not
  upgraded (not even direct requirements) unless the currently installed
  version fails to satisfy a requirement (either explicitly specified or a
  dependency).

  * This is actually the "default" upgrade strategy when ``--upgrade`` is
    *not set*, i.e. ``pip install AlreadyInstalled`` and
    ``pip install --upgrade --upgrade-strategy=to-satisfy-only AlreadyInstalled``
    yield the same behavior.

``--force-reinstall``

Exclude installed distributions from candidate selection and install the
selected distributions again, even if the versions match. This applies even
without ``--upgrade``.

``--ignore-installed``

Resolve as if installed distributions were absent, and do not uninstall them
before writing the selected distributions.

These options apply to both nab providers. Ordinary installed-package requests
can use the fast path without ``--ignore-installed``. See
:doc:`../../topics/more-dependency-resolution` for the automatic fallback rules.


Controlling what gets considered
================================

These options affect the list of distribution files that the resolver will
consider as candidates for installation. As such, they affect the data that
the resolver has to work with, rather than influencing what pip does with the
resolution result.

Prereleases

``--pre``

Source vs Binary

``--no-binary``

``--only-binary``

``--prefer-binary``

Wheel tag specification

``--platform``

``--implementation``

``--abi``

Index options

``--index-url``

``--extra-index-url``

``--no-index``

``--find-links``


Controlling dependency data
===========================

These options control what dependency data the resolver sees for any given
package (or, in the case of ``--python-version``, the environment information
the resolver uses to *check* the dependency).

``--no-deps``

``--python-version``

``--ignore-requires-python``


Special cases
=============

These need further investigation. They affect the install process, but not
necessarily resolution or what gets installed.

``--require-hashes``

``--constraint``

``--editable <LOCATION>``


.. _still evolving: https://github.com/pypa/pip/issues/8115
