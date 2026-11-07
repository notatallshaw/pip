# Resolver source snapshot

The base distribution is nab-resolver 0.0.26. The patch selects source from `nab-resolver/src/nab_resolver` at signed Nab commit `9810c84c2d9485bbdb220972764894978031b54b` (tree `853724456ed5afe7d6ad1fe257ce2b0627925b19`), based on main `53112670b7ec73f98bd986bd290406b954792627`. Pip's vendoring tool redirects imports to `pip._vendor`.

The subset includes predicate ranges, deferred availability, decision reconsideration, shared values, proof traversal and selection traversal. It omits `diagnostics.py`, `testing.py` and `version_spans.py`, and uses a minimal initializer. Pip supplies its own error rendering; the omitted modules remain in Nab.

These prototype changes are not released in 0.0.26.
