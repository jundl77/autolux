# Autoloops

One Lua file per look, `show/autoloops/<bank>/<loop>.lua`; banks are the intents
`atmospheric`, `breakdown`, `buildup`, `drop`. The API is `starfire/src/lua/lighting.lua`.

`standby/warm_white` is the exception: Autolux shows it while the input is digitally
silent (the player stopped). It is outside the rotation, required by readiness, and
deliberately static: no beat or loudness response, movers parked dark, warm white only.

Track boundaries and pauses come from the first live source, in this order. The Windows
system media session (the `media` extra; Spotify's session is preferred, else the current
one): a track change starts a new track when the delayed audio reaches it, and a pause or
stop drops the delayed audio and shows standby at once, re-arming on play. It counts as live
while it reported anything in the last 30 s. Otherwise the zeros in the audio: 2 s of
digital silence is standby, a 50 ms gap is a new-track cue, and an intro after a drop or
outro also starts a track.

## Rules

- **Bar-locked.** 8- or 16-beat loops, a pure function of `ctx.beat_position`, so
  switching never restarts a phrase. Starfire anchors that grid to the tracker's bar phase:
  `floor(beat_position) % ctx.beats_per_bar` is the beat in bar (0 is the one).
  For bar logic prefer the ctx form of the helpers — `sf.beat_in_bar(ctx)`, `sf.bar(ctx)`,
  `sf.phrase_phase(ctx, bars)` — which read `ctx.bar_phase` (continuous, 0..beats_per_bar),
  `ctx.bar_index` and `ctx.beats_per_bar`; the number form assumes 4/4.
  Only `buildup` may also use `ctx.autoloop_elapsed_beats`, for a ramp that plateaus at 128 beats.
- **Palette slots only**: `sf.palette()` primary/secondary/accent/cold/black of the active
  palette, which Autolux chooses per track and Starfire crossfades live
  (`sf.mix` of two slots is fine). No literal colours.
- **Loudness is amplitude only.** `sf.loudness_gain(ctx.normalized_rms_level)` and RMS reach
  scale levels and sweep size, never colour, timing or pattern.
- **No strobe.** Autolux adds the drop strobe itself.
- **Living room.** Slow symmetric mover sweeps (mirror with `sf.side(index)`, centre pan 0.5),
  nothing frantic, wash peaks at or below 0.9. Groups: `movers` (2), `wash` (4), `strip` (8);
  accent groups (MVR layer `accent`, such as `fx`) are refused, they play specials only.
- **Small.** At most ~80 lines. The first line is `-- 8-beat loop` or `-- 16-beat loop`
  (the tests read the loop length from it), then the metadata block. No other comments:
  encode intent in names.

## Metadata

Right after the loop-length line, exactly these three lines (any order), which Autolux uses to
choose a loop when an intent starts. Both Autolux readiness and the Starfire tests reject a
missing key or an unknown value.

```
-- energy: 2
-- movement: sweep
-- texture: wash
```

The metadata is structure only; colour identity belongs to the palette Autolux picks per track.

- `energy`: baseline intensity. `1` calm, `2` driving, `3` maximal. Atmospheric and breakdown
  stay at or below 2; drops are 2 or 3; a buildup's energy is the drop it leads into.
- `movement`: mover vocabulary. `still` (parked or barely drifting), `sweep` (continuous motion),
  `snap` (jumps between positions).
- `texture`: which roles carry the look. `wash` (the wash group), `beam` (the movers),
  `pixel` (patterns along the strip), `mixed` (no single role leads).

## Adding a loop

Drop the file into its bank directory and run `cargo test -p starfire --test autoloop_replay`.
The tests discover it, replay it (the bank default `tests/replays/<bank>.json`, unless
`tests/replays/<bank>/<loop>.json` exists) and run the per-bank semantic checks.

Golden DMX digests guard the renderer pipeline, one loop per bank
(`tests/golden/<bank>/<loop>.sha256`); new loops need no golden. After an intended render
change, re-record the existing goldens with `STARFIRE_WRITE_GOLDEN=1`.
