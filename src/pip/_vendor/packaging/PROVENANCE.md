# Packaging snapshot used by the nab integration

The base distribution is packaging 26.3. `tools/vendoring/patches/packaging.patch` reproduces the integration's patched snapshot through pip's normal vendoring command. This is a prototype bridge, not an unmodified packaging release.

The module's version label stays at the base release so pip's vendor inventory and debug output agree.

The implementation was imported from `nab-provider/src/nab_provider/_vendor/packaging` in notatallshaw/nab at `b88405a017806d8f7362f4c8691b9f3b78a8c26d`. That snapshot includes version-range operations and prepared marker evaluation used by the integration. Its upstream pin, accumulated patch and original provenance are in that nab revision; their task paths refer to nab, not this pip checkout.

Keep runtime changes separate from vendoring reproducibility changes. Regenerate the pip patch from pristine vendored packaging 26.3 and the intended snapshot, then run `nox -s vendoring` and verify that it leaves the committed tree unchanged. Do not hand-edit the generated patch or silently replace the snapshot with another revision.

The upstream Apache-2.0 and BSD-2-Clause license texts remain alongside the source. Packaging unvendoring is outside this prototype.
