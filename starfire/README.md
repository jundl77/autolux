# Starfire

Rust lighting renderer with banked Lua sequences, fixture-aware composition and
deterministic DMX replay. Autolux chooses sequences and accents; Radius owns
synchronized transport. Production SoundSwitch remains available separately.

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
The supplied sequences expect `wash` and `movers` groups in the rig.
Selecting the current bank/name keeps it running. Overrides affect only their
properties; clearing reveals the continuing baseline.

Every sequence has a checked full-DMX replay plus behavior assertions. Run
`cargo test --locked --manifest-path starfire/Cargo.toml`. The build also runs
rustfmt, Clippy and release compilation. Toolchain and formatting are pinned at
the repository root.

The room rig includes verified Spot 110 and SePar profiles. Other hardware,
physical calibration and live Radius clock integration require review before
production use. See the [design and diagrams](../docs/starfire-design.md) and
[decision log](../docs/starfire/decisions.md).
See [validation](../docs/starfire/validation.md) for tested behavior and hardware limits.
