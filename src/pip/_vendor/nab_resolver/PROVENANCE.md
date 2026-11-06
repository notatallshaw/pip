# Resolver SDK snapshot

The base distribution is nab-resolver 0.0.26. The vendoring patch reproduces `nab-resolver/src/nab_resolver` at Nab commit `bf83840356d940aa863ac0abf709e6f1d9e87855`, with imports redirected to `pip._vendor`.

The candidate-provider, deferred-query and scheduling APIs come from SDK commit `712a9c08efdb95091b08573e40828c4e316807ed`. Shared value, keyed-range, proof, selected-graph and version-span changes were composed from the native API delta `2d54f3b4ac18ba50d94dc220e8ce6b35a2eca695` to `97d72ef02ac4fd0b7bcc240db6129ec1406af027` using Git's three-way merge. The SDK's other source files retain their earlier implementations.

These APIs are prototype patches, not released in 0.0.26. The separately sourced candidate preference subset is recorded in `nab_provider/PROVENANCE.md`; the two selected snapshots are verified together.
