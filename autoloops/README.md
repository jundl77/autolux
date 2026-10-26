# Autoloops

One Lua file per look, `autoloops/<bank>/<loop>.lua`; banks are the intents
`atmospheric`, `breakdown`, `buildup`, `drop`. The API is `starfire/src/lua/lighting.lua`.

`standby/warm_white` is the exception: Autolux shows it while the input is digitally
silent (the player stopped). It is outside the rotation, required by readiness, and
deliberately static: no beat or loudness response, movers parked dark, warm white only.

## Rules

- **Bar-locked.** 8- or 16-beat loops, a pure function of `ctx.beat_position`, so
  switching never restarts a phrase. Starfire anchors that grid to the tracker's bar phase:
  `floor(beat_position) % ctx.beats_per_bar` is the beat in bar (0 is the one).
  For bar logic prefer the ctx form of the helpers — `sf.beat_in_bar(ctx)`, `sf.bar(ctx)`,
  `sf.phrase_phase(ctx, bars)` — which read `ctx.bar_phase` (continuous, 0..beats_per_bar),
  `ctx.bar_index` and `ctx.beats_per_bar`; the number form assumes 4/4.
  Only `buildup` may also use `ctx.autoloop_elapsed_beats`, for a ramp that plateaus at 128 beats.
- **Palette slots only**: `sf.palette("techno")` primary/secondary/accent/cold/black
  (`sf.mix` of two slots is fine). No literal colours.
- **Loudness is amplitude only.** `sf.loudness_gain(ctx.normalized_rms_level)` and RMS reach
  scale levels and sweep size, never colour, timing or pattern.
- **No strobe.** Autolux adds the drop strobe itself.
- **Living room.** Slow symmetric mover sweeps (mirror with `sf.side(index)`, centre pan 0.5),
  nothing frantic, wash peaks at or below 0.9. Groups: `movers` (2), `wash` (4), `strip` (8).
- **Small.** At most ~80 lines. The header comment's first line is `-- <8|16>-beat loop...`
  (the tests read the loop length from it), then the metadata block, then one line per group,
  then `Palette slots: ...`.

## Metadata

Right after the loop-length line, exactly these three lines (any order), which Autolux uses to
choose a loop when an intent starts. Both Autolux readiness and the Starfire tests reject a
missing key or an unknown value.

```
-- energy: 2
-- family: warm
-- movement: sweep
```

- `energy`: baseline intensity. `1` calm, `2` driving, `3` maximal. Atmospheric and breakdown
  stay at or below 2; drops are 2 or 3; a buildup's energy is the drop it leads into.
- `family`: dominant colour character. `cold` (cool white), `warm` (warm white/amber), `accent` (red).
- `movement`: mover vocabulary. `still` (parked or barely drifting), `sweep` (continuous motion),
  `snap` (jumps between positions).

## Adding a loop

1. Drop the file into its bank directory. The tests in `starfire/tests/autoloop_replay.rs`
   discover it, replay it (the bank default `tests/replays/<bank>.json`, unless
   `tests/replays/<bank>/<loop>.json` exists) and run the per-bank semantic checks.
2. Run `cargo test -p starfire --test autoloop_replay`; it fails only for the missing golden.
3. Record it with `STARFIRE_WRITE_GOLDEN=1 cargo test -p starfire --test autoloop_replay`.
   This writes `tests/golden/<bank>/<loop>.sha256` and never overwrites an existing golden.
