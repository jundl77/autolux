# Autolux development

- `cli/` owns lifecycle and deployment; `autolux/` owns analysis, control and data;
  `radius/` owns transport and hardware output; `starfire/` owns Rust/Lua lighting
  rendering; `proto/` owns shared protocol contracts. Keep tests with each component.
- Keep READMEs and AGENT files brief, describing current intent and contracts.
- Fix discovered bugs and add meaningful regression tests.
- Focus tests on transport, synchronization, lifecycle, model output and data
  integrity. Do not add UI, configuration snapshot or static source-shape tests.
- Build scripts and pre-commit hooks run Ruff, clang-format and rustfmt directly
  using saved policy; Rust builds also run Clippy. Development helpers belong in root `tools/`,
  never business packages; do not add unused scaffolding or boilerplate.
- Review new or changed naming concepts with the personal `blind-naming-test`
  skill: fresh low-effort reviewers see names only; compare their explanations
  against the code, rename mismatches and retest without revealing the answer.
  Cap reviews at important domain/API names, units and invariants; skip routine locals.

## Runtime

Live shows and simulations share the analysis/decoder chain. Online rhythm drives
bar timing; MERT and the section model produce immutable bar decisions. Only
committed decisions select lighting intents. Startup and silence are explicit.

Apply the show delay once, in TX audio and scheduled controller commands.
Radius carries a session ID and 64-bit audio-frame watermark. RX schedules DMX
against audible audio; jitter buffering is separate from the show delay.

Audio callbacks must not allocate, log, block or perform network/USB I/O.
Use bounded queues with clear ownership. Keep audio and DMX output on separate
threads. Both endpoints send 10-second heartbeats and fail after 30 seconds.

The audio loop owns controller state. GPU work and the dashboard are isolated.
GPU faults hold intent while rhythm continues. Production refuses missing models.
Report missing/stale measurements explicitly; never present them as healthy zeros.

Own only processes started by the session. Validate configuration, verify SSH
host keys, wait for readiness, unwind partial starts and bound shutdown.

## Data and checks

Resolve local assets through `corpus_root`; honor `RAVEFORM_DATA_DIR`. Commit
fixtures and hand labels, never models, corpora, generated caches, logs or keys.
Preserve label vocabulary, artifact provenance, split isolation and benchmark
exclusions. Never rebaseline to hide a regression. Check external training-module
origins explicitly. Serialize GPU workloads.

Before publishing, run `uv run pytest`, `.\build-radius.ps1` and the committed
backtest; exercise simulation and live hardware when their paths change. Use a
branch and PR unless the user explicitly requests a direct push. State validation
limits accurately.
