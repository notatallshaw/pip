# Resolver source snapshot

The base distribution is nab-resolver 0.0.26. The patch selects source from `nab-resolver/src/nab_resolver` at signed Nab commit `2158df2745d1e1c0bd75fc133335b1e817405e34` (tree `84bd7cd9b117ef4088232db57edbc64a52af38ca`), based on main `53112670b7ec73f98bd986bd290406b954792627`. Pip's vendoring tool redirects imports to `pip._vendor`.

The subset includes preferred resolution, deferred availability, shared values, proof traversal and selection traversal. It omits `diagnostics.py`, `testing.py` and `version_spans.py`, and uses a minimal initializer. Pip supplies its own error rendering; the omitted modules remain in Nab.

These prototype changes are not released in 0.0.26.
