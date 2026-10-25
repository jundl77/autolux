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
`ctx.normalized_rms_level` is measured audio RMS normalized to 0..1, separate from master
brightness; bundled sequences use it for restrained brightness variation.
The supplied sequences expect `wash` and `movers` groups in the rig.
Selecting the current bank/name keeps it running. Overrides affect only their
properties; clearing reveals the continuing baseline.

Every sequence has a checked full-DMX replay plus behavior assertions. Run
`cargo test --locked --manifest-path starfire/Cargo.toml`. The build also runs
rustfmt, Clippy and release compilation. Toolchain and formatting are pinned at
the repository root.

Replay reports `render_elapsed_seconds` and `render_realtime_factor`: a factor
of 20 means 20 seconds of show rendered per wall-clock second. This includes
Lua and fixture encoding, excluding file output and network pacing. Performance
tests require 10x for the room rig and 2x for 64 fixtures over a 90-second show.

Live mode receives framed Protobuf commands through owned process pipes and
renders Art-Net at 40 Hz. Heartbeats expire after 30 seconds; EOF and failures
black out output. The contract is in `../proto/starfire/starfire.proto`.

The room rig includes verified Spot 110 and SePar profiles. Other hardware,
physical calibration and exact Radius clock handoff require review.
See the [design and diagrams](../docs/starfire-design.md) and
[decision log](../docs/starfire/decisions.md).
See [validation](../docs/starfire/validation.md) for tested behavior and hardware limits.
