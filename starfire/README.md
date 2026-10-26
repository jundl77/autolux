# Starfire

Rust lighting renderer with banked Lua autoloops, fixture-aware composition and
deterministic DMX replay. Autolux chooses autoloops and accents; Radius owns
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
`target/release/starfire.exe --help`.

Authored looks live in root `autoloops/`, grouped by bank. Autoloops return semantic
effects from `render(ctx)`. Use supplied musical time,
named groups and helpers in `src/lua/lighting.lua`; never depend on render count.
`ctx.normalized_rms_level` is measured audio RMS normalized to 0..1, or `nil` until
measured. Bundled autoloops keep their baseline brightness when it is unknown.
The supplied autoloops expect `wash`, `movers` and `strip` groups in the rig.
Selecting the current autoloop ID keeps it running. Overrides affect only their
properties; clearing reveals the continuing baseline. Global brightness,
animation speed, blackout and measured audio level use typed parameters.

Every autoloop has a checked full-DMX replay plus behavior assertions. Run
`cargo test --locked --manifest-path starfire/Cargo.toml`. The build also runs
rustfmt, Clippy and release compilation. Toolchain and formatting are pinned at
the repository root.

Replay reports `render_elapsed_seconds` and `render_realtime_factor`: a factor
of 20 means 20 seconds of show rendered per wall-clock second. This includes
Lua and fixture encoding, excluding file output and network pacing. Performance
tests require 10x for the room rig and 2x for 64 fixtures over a 90-second show.

Live mode starts without output resources. Autolux supplies the complete typed
fixture patch, profiles, banked Lua sources, Radius transport settings and
initial parameters in `Login`. Starfire validates every script against this rig
before `LoginReply` confirms readiness at 40 Hz; live mode loads no asset files.
Production runs Radius TX in an owned worker and passes DMX directly to it.
Standalone rendering can instead use an explicit unicast Art-Net destination.
Closing the session blacks out and releases output. Independent heartbeats run
both ways every ten seconds and expire after thirty seconds. Only login has a
success reply; rejected commands produce `ErrorReply`, and `Metrics` reports the
last processed command ID. Standalone messages are generated from [starfire.proto](../proto/starfire/starfire.proto).
Each frame carries a message type, bounded length and generated Protobuf payload.
An empty `PlayAutoloop` enters idle; no autoloop restarts on repeated selection.

The room rig `rigs/room.mvr` has two Spot 110
movers, four SePar washes and eight RGB cells of
the CF805 in 24-channel mode. These are 14 independently controlled fixtures.
Mini Kinta remains dark until its control profile is verified. Production
readiness probes every autoloop against the selected patch before deployment.
Physical timing measurements are described in the validation record.
See the [design and diagrams](../docs/starfire-design.md) and
[decision log](../docs/starfire/decisions.md).
See [validation](../docs/starfire/validation.md) for tested behavior and hardware limits.

Fixture types use standard GDTF XML and a selected DMX mode. Import a `.gdtf`
archive with `uv run autolux starfire import-fixture fixture.gdtf --profile-id spot --mode "16ch"`.
The renderer validates the mode before saving it in the `rigs/profiles.json`
library. Rigs are MVR files (`rigs/room.mvr`) that embed the GDTF files they use.
Autolux parses them; the native `check`, `rig` and `replay` commands take the exported
`RigDefinition` (`autolux starfire export-rig room.mvr room.rig.pb`; the Autolux CLI
exports automatically). Moving-head homes come from
each fixture's MVR Matrix ([rig-mvr](../docs/starfire/rig-mvr.md)). Patch and
place fixtures in the existing Config tab.

Lua `sf.physical_attribute(target, "Zoom", 25)` uses the GDTF physical unit;
`sf.channel_preset(target, "Gobo1", "Open")` selects a named channel preset.
Qualify ambiguous properties as `Geometry.Attribute` or a full function path.
The same controls are available through active effects. Explicit attributes
win over convenience values on the same channel; master and blackout apply last. Standard colour,
intensity and calibrated movement helpers remain convenient for portable looks.
Unsupported GDTF constructs reject during validation; this is not full GDTF
conformance. See the [decision log](../docs/starfire/decisions.md) for limits.
