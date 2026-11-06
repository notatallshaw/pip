# Resolver snapshot

The base distribution is nab-resolver 0.0.26. The vendoring patch reproduces `nab-resolver/src/nab_resolver` at Nab commit `712a9c08efdb95091b08573e40828c4e316807ed`, based on main `066b2d4388c6838ff5e9d0ba271857c36bd02999`, with imports redirected to `pip._vendor`.

The source includes the merged generic provider checks and current resolver changes. Its additional candidate-provider, deferred-query and scheduling APIs are prototype patches, not released APIs. The candidate preference subset is sourced independently from the main-based provider API recorded in `nab_provider/PROVENANCE.md`.
