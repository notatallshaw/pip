# Terminal output comparison

These GIFs show actual `python -m pip` commands recorded in pseudo-terminals with 80 columns. The paired `.cast` files contain the captured terminal output and timing. The recordings use CPython 3.14 on Linux, the same terminal settings, and matching offline package inputs.

The download commands use a local HTTP server, with and without a Content-Length header. Installation uses local wheels. The recursive build installs local build dependencies and runs PEP 517 hooks. The invalid build-system requirement command exits with status 1; the other commands succeed.

`manifest.json` identifies both source revisions and the recording dimensions. The GIFs play at the captured speed, with a final pause before looping. Help and JSON complete in a single output burst; download, installation and build recordings show their actual updates.
