# Packaging upstream snapshot

The base distribution is packaging 26.3. The prototype's vendoring patch replaces its source with upstream packaging commit `713d0d78924ab2686c513dde058ce6aafc63e766`. The module version stays at 26.3 to agree with the released distribution in pip's vendor inventory; the selected upstream source identifies itself as 26.4.dev0.

The source includes the public version-range API used by the resolver. No Nab-specific packaging features are retained. The only source change from the upstream commit is the inventory version label; pip's vendoring tool applies its normal namespace transformations.

Regenerate `tools/vendoring/patches/packaging.patch` from pristine packaging 26.3 and the selected upstream source, then verify it through `nox -s vendoring`. This unreleased snapshot remains a prototype dependency. The upstream Apache-2.0 and BSD-2-Clause license texts remain beside the source.
