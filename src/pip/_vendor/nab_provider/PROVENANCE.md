# Provider preference snapshot

The base distribution is nab-provider 0.0.26. Pip retains only its initializer, typing marker, license files and supported preference module. The full provider and universal-resolution code are excluded by the vendoring configuration.

The preference implementation comes from `nab-provider/src/nab_provider/preference.py` at Nab commit `478767013c8f9fe0763481ecb965baada69aab04`. The vendoring patch adds those exact source bytes with imports redirected to `pip._vendor.nab_resolver` and a minimal package initializer. It does not copy Python candidate or index policy.

The base distribution declares nab-resolver 0.0.26 and nab-markersets dependencies for the complete provider. This selected module uses only the generic resolver API at runtime. Pip supplies its existing patched nab-resolver 0.0.17 snapshot, recorded by `tools/vendoring/patches/nab-resolver.patch`; the two snapshots are verified together. The subset does not import nab-markersets or typing_extensions at runtime.
