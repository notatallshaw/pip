# Provider candidate preference snapshot

The base distribution is nab-provider 0.0.26. Pip retains its supported candidate preference modules and the record and compatibility helpers they use. The index-backed provider and universal-resolution modules are excluded.

The selected source comes from `nab-provider/src/nab_provider` at Nab commit `ef457fec8dd69d5073247cff015773dbe7e31b79`, based on main `53112670b7ec73f98bd986bd290406b954792627`. The vendoring patch reproduces `_compat.py`, `preference.py`, `preference_provider.py`, `value.py` and `version_candidates.py`, with host packaging imports, imports redirected to `pip._vendor`, and a minimal initializer.

These APIs are prototype patches, not released in 0.0.26. The complete distribution declares nab-markersets, but this subset does not import it or typing_extensions at runtime. Pip supplies the separately composed resolver SDK recorded in `nab_resolver/PROVENANCE.md`; the two selected snapshots are verified together.
