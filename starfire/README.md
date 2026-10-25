# Starfire

Rust lighting renderer with banked Lua sequences, fixture-aware composition and
deterministic DMX replay. Autolux chooses sequences and accents; Radius owns
synchronized transport.

```powershell
.\build-starfire.ps1
uv run autolux starfire list
uv run autolux starfire replay starfire/tests/replays/drop.json --output .live/drop.dmx
uv run autolux starfire ui
```

Replay is offline unless an explicit Art-Net destination and real-time pacing
are requested. Fixture setup uses the existing dashboard; saved patches apply
next session. Native commands are also available in
`starfire/target/release/starfire.exe --help`.

Sequences return semantic effects from `render(ctx)`. Use supplied musical time,
named groups and helpers in `src/lua/lighting.lua`; never depend on render count.
`ctx.normalized_rms_level` is measured audio RMS normalized to 0..1, or `nil` until
measured. Bundled sequences keep their baseline brightness when it is unknown.
The supplied sequences expect `wash`, `movers` and `strip` groups in the rig.
Selecting the current bank/name keeps it running. Overrides affect only their
properties; clearing reveals the continuing baseline. Optional Lua `parameters`
declare `value_type`, `default_value`, bounds and choices. Read the active values
through `ctx.parameters`; typed updates validate before changing a value.

Every sequence has a checked full-DMX replay plus behavior assertions. Run
`cargo test --locked --manifest-path starfire/Cargo.toml`. The build also runs
rustfmt, Clippy and release compilation. Toolchain and formatting are pinned at
the repository root.

Replay reports `render_elapsed_seconds` and `render_realtime_factor`: a factor
of 20 means 20 seconds of show rendered per wall-clock second. This includes
Lua and fixture encoding, excluding file output and network pacing. Performance
tests require 10x for the room rig and 2x for 64 fixtures over a 90-second show.

Live mode starts without output resources. `Login` supplies rig, sequences,
Art-Net destination and initial typed parameters; a successful reply owns one
session rendering at 40 Hz. Closing the session blacks out and releases output.
Heartbeats run both ways every ten seconds and expire after thirty seconds.
Commands and replies are generated from [starfire.proto](../proto/starfire/starfire.proto).
An empty `PlaySequence` enters idle; no sequence restarts on repeated selection.

The room rig has two Spot 110 movers, four SePar washes and eight RGB cells of
the CF805 in 24-channel mode. These are 14 independently controlled fixtures.
Mini Kinta remains dark until its control profile is verified. Production
readiness probes every sequence against the selected patch before deployment.
Physical timing measurements are described in the validation record.
See the [design and diagrams](../docs/starfire-design.md) and
[decision log](../docs/starfire/decisions.md).
See [validation](../docs/starfire/validation.md) for tested behavior and hardware limits.
