# Native callback source snapshot

The base distribution is nab-provider 0.0.26. The patch selects `_compat.py`, `callbacks.py`, `candidate_stream.py` and `py.typed` from `nab-provider/src/nab_provider` at signed Nab commit `9810c84c2d9485bbdb220972764894978031b54b` (tree `853724456ed5afe7d6ad1fe257ce2b0627925b19`), based on main `53112670b7ec73f98bd986bd290406b954792627`. The resolver uses the same source commit.

Pip retains native matching, acquisition, input policy and installation. The provider subset supplies native candidate queries, dependency scopes and original failure causes. A minimal initializer omits the index-backed and version-catalogue providers, universal resolution and the private packaging copy. The original license files are retained.

The prototype APIs are not released in 0.0.26. The matching resolver is recorded in `nab_resolver/PROVENANCE.md`.
