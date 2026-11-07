# Provider preference and prepared-resolution snapshot

The base distribution is nab-provider 0.0.26. The patch selects `_compat.py`, `preference.py`, `preference_provider.py`, `prepared_resolution.py`, `value.py`, `version_candidates.py` and `py.typed` from `nab-provider/src/nab_provider` at signed Nab commit `2158df2745d1e1c0bd75fc133335b1e817405e34` (tree `84bd7cd9b117ef4088232db57edbc64a52af38ca`), based on main `53112670b7ec73f98bd986bd290406b954792627`. The resolver uses the same source commit.

Pip uses a minimal initializer and its own packaging namespace. Its vendoring tool redirects absolute imports to `pip._vendor`. The original license files are retained. The index-backed provider, universal resolver and private packaging copy are excluded; this subset needs neither nab-markersets nor typing_extensions at runtime.

The subset includes candidate-consideration callbacks and exact conditional dependencies. These prototype APIs are not released in 0.0.26. The matching resolver is recorded in `nab_resolver/PROVENANCE.md`.
