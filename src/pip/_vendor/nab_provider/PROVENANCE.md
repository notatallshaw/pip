# Provider candidate preference snapshot

The base distribution is nab-provider 0.0.26. Pip retains its supported candidate preference modules and the record and compatibility helpers they use. The index-backed provider and universal-resolution modules are excluded.

The selected source comes from `nab-provider/src/nab_provider` at Nab commit `2d54f3b4ac18ba50d94dc220e8ce6b35a2eca695`, based on main `f7a4b51609494f8d62b74606f1a2e6ef3d7d81a6`. The vendoring patch reproduces `_compat.py`, `preference.py`, `preference_provider.py`, `value.py` and `version_candidates.py`, with imports redirected to `pip._vendor` and a minimal initializer.

These prototype APIs are not released in 0.0.26. The complete distribution declares nab-markersets, but this subset does not import it or typing_extensions at runtime. Pip supplies the separately sourced resolver SDK recorded in `nab_resolver/PROVENANCE.md`; the two selected snapshots are verified together.
