# Terminal output comparison

These images show actual `python -m pip` commands recorded in 80-column pseudo-terminals on Linux with CPython 3.14. Paired captures use matching terminal settings and package inputs. The `.cast` files retain the original output and timing; `manifest.json` identifies the source revisions and previews.

Downloads use a localhost HTTP server, with and without Content-Length. Installation uses local wheels. The wheel-build example uses a backend that waits before returning its wheel. Recursive builds install local build dependencies through pip's default CLI path. The invalid build-system requirement command exits with status 1; the other commands succeed.

Animated previews start at the first output event and run at the captured speed, with a final pause before looping. Help, JSON and diagnostics have still PNG previews. The original recordings retain startup timing.
