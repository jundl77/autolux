# Autolux Python

- Analysis produces evidence; the decoder commits decisions; controllers select
  outputs. Keep those responsibilities explicit and functions focused.
- Follow Ruff settings in `pyproject.toml`. Review new or changed names with the
  personal `blind-naming-test` skill; prefer domain meaning and explicit units.
- No magic values. Name domain thresholds, sizes, rates and timeouts near their
  owner. Reuse constants and configuration rather than duplicating literals.
- Validate external input at boundaries. Handle specific exceptions, preserve
  their cause and surface actionable failures. Do not silently invent defaults
  for missing measurements or catch errors just to continue.
- Use explicit ownership, bounded queues/caches and deterministic cleanup.
  Keep GPU work and dashboards off the audio loop; preserve live/replay parity.
- Prefer straightforward code and small interfaces. Avoid speculative layers,
  repeated boilerplate and comments that merely narrate the implementation.
- Prefer deleting unnecessary code to adding helpers. Share code only when the
  common behavior is real; do not trade readable control flow for compactness.
- Keep development helpers in root `tools/`, not app packages. Keep tests with
  their component, add regressions for bugs, and never alter baselines to pass.
