# Audio analysis

Use online rhythm to produce beat instants and tempo from the captured stream.
Timestamp events from sample time; keep detector framing inside its adapter.

MERT, the section model and bar tracking consume bounded audio history on the
GPU worker. Artifact geometry and checksums define their contracts. The decoder
owns immutable bar decisions; analysis stages must not select lighting effects.

Keep the audio loop responsive. Cross-thread handoffs are bounded. On GPU faults,
continue feeding the sample timeline, report degradation, hold section decisions
and recover with cleared stale state. Optional bar-tracker failure falls back to
the counting grid without stopping rhythm.

Live and simulated analysis must remain equivalent. Add regression tests under
`autolux/tests/` and preserve the committed benchmark.
