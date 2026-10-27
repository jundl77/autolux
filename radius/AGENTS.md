# Radius Rust

- Keep one embedded TX worker and one RX. Transport owns media delivery; hardware output stays
  separate from analysis and application lifecycle.
- Follow rustfmt; use clear ownership, small functions and space between concepts.
  Use the personal `blind-naming-test` skill when reviewing new or changed names.
- No magic values. Name protocol sizes, capacities, rates, thresholds and timeouts
  next to their owner; include units in names. Reuse existing constants.
- Make ownership and lifetime explicit. Prefer RAII, value types and bounded
  queues; keep interfaces small and avoid speculative abstractions. Unsafe code is limited to
  the Win32 scheduling calls in `realtime.rs`.
- Prefer deletion and direct code over new layers. Reduce duplication without
  making the control flow harder to follow; fewer lines alone is not readability.
- Validate lengths, ranges and overflow before access or conversion. Preserve
  run-ID and media-sequence ordering. Fail with actionable errors; never silently discard
  a failure or report missing measurements as zero.
- Audio callbacks must not allocate, log, block or perform network/USB I/O.
  Keep queue ownership explicit and DMX on its own worker. Bound every wait.
- Apply show delay once; schedule lighting against audible audio. Preserve
  bidirectional 10-second heartbeats and the 30-second session timeout.
- Keep development helpers in root `tools/`, tests in `radius/tests/`, and fix
  bugs with regression tests. Run cargo test, rustfmt and Clippy.
