# Resolver source snapshot

The base distribution is nab-resolver 0.0.26. The patch selects source from `nab-resolver/src/nab_resolver` at signed Nab commit `f5550702cb034e8908ce45d463d1ab6006bb2d34` (tree `408328524c3e75b0120d8df8d7ddbe5b60992a24`), based on main `3a92b9ad22a15d12f563608864d004145984b312`. Pip's vendoring tool redirects imports to `pip._vendor`.

The subset includes predicate ranges, deferred availability, decision reconsideration, shared values, proof traversal and selection traversal. It omits `diagnostics.py`, `testing.py` and `version_spans.py`, and uses a minimal initializer. Pip supplies its own error rendering; the omitted modules remain in Nab.

These prototype changes are not released in 0.0.26.
