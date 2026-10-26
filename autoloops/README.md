# Autoloops

One Lua file per look, `autoloops/<bank>/<loop>.lua`; banks are the intents
`atmospheric`, `breakdown`, `buildup`, `drop`. The API is `starfire/src/lua/lighting.lua`.

## Rules

- **Bar-locked.** 8- or 16-beat loops, a pure function of `ctx.beat_position`
  (beat 0 and every multiple of 4 is the one), so switching never restarts a phrase.
  Only `buildup` may also use `ctx.autoloop_elapsed_beats`, for a ramp that plateaus at 128 beats.
- **Palette slots only**: `sf.palette("techno")` primary/secondary/accent/cold/black
  (`sf.mix` of two slots is fine). No literal colours.
- **Loudness is amplitude only.** `sf.loudness_gain(ctx.normalized_rms_level)` and RMS reach
  scale levels and sweep size, never colour, timing or pattern.
- **No strobe.** Autolux adds the drop strobe itself.
- **Living room.** Slow symmetric mover sweeps (mirror with `sf.side(index)`, centre pan 0.5),
  nothing frantic, wash peaks at or below 0.9. Groups: `movers` (2), `wash` (4), `strip` (8).
- **Small.** At most ~80 lines. The header comment's first line is `-- <8|16>-beat loop...`
  (the tests read the loop length from it), then one line per group, then `Palette slots: ...`.

## Adding a loop

1. Drop the file into its bank directory. The tests in `starfire/tests/autoloop_replay.rs`
   discover it, replay it (the bank default `tests/replays/<bank>.json`, unless
   `tests/replays/<bank>/<loop>.json` exists) and run the per-bank semantic checks.
2. Run `cargo test -p starfire --test autoloop_replay`; it fails only for the missing golden.
3. Record it with `STARFIRE_WRITE_GOLDEN=1 cargo test -p starfire --test autoloop_replay`.
   This writes `tests/golden/<bank>/<loop>.sha256` and never overwrites an existing golden.
