# Packaging snapshot used by the nab integration

The base distribution is packaging 26.3. `tools/vendoring/patches/packaging.patch` reproduces the integration's patched snapshot through pip's normal vendoring command. This is a prototype bridge, not an unmodified packaging release.

The implementation was imported from `nab-provider/src/nab_provider/_vendor/packaging` in notatallshaw/nab at `865785398361c122bf1b00c85f87755c43e6f4d5`. That snapshot includes version-range operations and prepared marker evaluation used by the integration. Its upstream pin, accumulated patch and original provenance are in that nab revision; their task paths refer to nab, not this pip checkout.

Keep runtime changes separate from vendoring reproducibility changes. Regenerate the pip patch from pristine vendored packaging 26.3 and the intended snapshot, then run `nox -s vendoring` and verify that it leaves the committed tree unchanged. Do not hand-edit the generated patch or silently replace the snapshot with another revision.

The upstream Apache-2.0 and BSD-2-Clause license texts remain alongside the source. Packaging unvendoring is outside this prototype.
