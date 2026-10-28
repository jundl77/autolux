# Autoloops

One Lua file per look, `show/autoloops/<bank>/<loop>.lua`. The API is
`starfire/src/lua/lighting.lua` plus the native bindings in `starfire/src/script.rs`.

- **Base banks** are the intents `atmospheric`, `breakdown`, `buildup` and `drop`. A section
  cue plays a rotation of base loops, one at a time, each for a whole number of bars.
- **Overlay banks** are `hit`, `fill`, `riser`, `dip` and `accent`. A cue schedules them above
  its base, once or every few bars, each with a begin, a length and an attack and release
  envelope.

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

## Show grammar

A track is a few sections, each a cue: intro (atmospheric), build, drop, breakdown, build,
drop, outro. Inside a section the look changes on phrase lines, never on a random beat.

- **Base.** One loop at a time; rotation entries change on downbeats.
- **Accents** run for 16 bars and change 8 bars offset from the base rotation, so the two never
  turn over together.
- **Fills** mark phrase ends: a small one on bar 8, a bigger one on bar 16.
- **Into a drop:** the build rises, a riser rides its last bars, the dip blacks out the last beat
  (half a beat to two bars, measured from the audio), and an entry hit lands on the one.
- **Into a breakdown:** `dip/fade` settles the room before the breakdown's base takes over.
- **Restraint means smaller, never absent.** Calm keeps the dip and bumps in a palette colour
  (no white, no strobe); Balanced allows white hits but no strobing riser; Wild adds strobe.

Contrast comes from the dip and from white, not from a cap. Base loops peak at 0.9 so a hit
above them still reads, but the punch of a drop is the dark beat before it and the white on
its one.

### Buildups: two regimes in every loop

Each buildup implements both, through one drive amount:

```lua
local drive = 0.15 + 0.55 * sf.tension(ctx, 64)          -- regime U: the drop is unknown
if ctx.next_intent == "drop" then                         -- regime K: the drop is cued
    local finale = sf.finale(ctx, 16)
    drive = drive + (1 - drive) * finale                  -- continuous hand-over to 1
elseif ctx.next_intent then
    drive = drive * (1 - 0.5 * sf.finale(ctx, 8))         -- easing out into a non-drop
end
```

- **Regime U** builds on the beats since the section start and plateaus at 0.7 after 64 beats,
  so an uncued build never peaks early or restarts.
- **Structural steps** (a rate doubling, a fill step) happen on downbeats: count them from
  `elapsed - ctx.bar_phase`, the beats at the last downbeat.
- **Regime K** takes over continuously when the drop cue arrives, however late. Counts move one
  step per beat (`math.ceil(ctx.beats_until_next)`).
- **Stillness and tightening into the last bar:** motion collapses, rates are at their fastest,
  the level is at its peak.
- **The last-beat dip comes from the cue's `dip` overlay, never from the loop.**

Every buildup is one idea that drives every layer, not a variation of parameters:

| Loop | Idea |
|---|---|
| `countdown` | The strip fills from the centre one step per bar, then counts back one step per beat over the last eight; beams descend onto their focus. |
| `tighten` | A fan of beams closes onto one shared point; the strip's two dots and the washes' light converge on the centre with it. |
| `stutter` | A roll whose pulses halve on downbeats (a bar, two beats, one, half, a quarter) on every layer at once. |
| `ramp` | A strip chase and the movers' circles double their speed together, then freeze solid for the last bar. |
| `pistons` | A sidechain pump that deepens with the build, doubles over the last two bars and locks solid in the last. |
| `inhale` | Two-bar breaths that deepen; once the drop is cued everything freezes at the top of the inhale. |

### The rest of the catalogue

- **Drops** pair a wash groove with a mover phaser and a strip chase, on a monochrome base with
  the second colour on accents: `kick`, `hammer`, `crossfire`, `piston_kick`, `rolling`
  (directional chase, circles), `wide_fan` (slow fan, centre-out waves), `ballyhoo` (Wild:
  30-degree ballyhoo, seeded sparkle) and `wall_split` (outer members in the second colour).
- **Breakdowns** are mono and dark-first, at most one swell a bar: `heartbeat`, `afterglow`,
  `cathedral`, `drift_apart`, `gobo_breakup` (a slow gobo circle in haze) and `one_pixel`
  (negative space: one breathing cell).
- **Atmospherics** are two colours and slow: `amber_drift`, `boiler_room`, `cercle`, `dawn`,
  `fog`, `orbit` (slow circles about the focus) and `tilt_tide` (a tide crossing the room).

## Overlays

| Bank | Loops | Header |
|---|---|---|
| `hit` | `blinder` (washes and strip white), `beam_punch` (every beam on one point, open gobo, white), `colour_bump` (Calm: the palette's punch colour), `strobe` (Wild: white strobe on every group) | HTP |
| `fill` | `strip_wipe` and `mover_flick` (bar 8), `turnaround` (bar 16: every group rolls, lands on a dark eighth) | LTP |
| `riser` | `strobe_ramp` (Wild: strobing white rise), `white_sweep` (beams lift into the air, washes whiten), `strip_fill` (the strip closes from the ends) | HTP |
| `dip` | `last_beat` (every group dark), `fade` (a low glow for breakdown entry) | LTP |
| `accent` | `sparkle` (seeded strip sparkle), `wash_roll` (washes take turns), `mover_shapes` (figure-eights through a gobo) | LTP |

An overlay's first line is free text saying what it does, then exactly these two lines:

```
-- strobe: yes|no
-- intensity: htp|ltp
```

- `strobe: yes` marks a strobing overlay; the director's venue gate counts it as an intense event.
- `intensity: htp` takes the higher of the look below and the overlay's own level (hits,
  risers); `ltp` replaces the look below on the groups it sets (fills, accents, dips). Colour,
  position and attributes always blend from the look below by the envelope; under HTP the
  colour follows whichever source wins intensity, so a white hit's colour decays with it.
- An overlay sets only the groups it is about; everything else shows the base.
- `ctx.autoloop_elapsed_beats` counts from the overlay's nominal begin and
  `ctx.overlay_length_beats` is its length, so `progress = elapsed / length`.
- **Strobe rates are beat divisions of `ctx.bpm`** (`hz = bpm / 60 * division`), stepped on
  beat lines and never above the 10 Hz cap: pick the fastest division under it. The engine
  gates the strobe in phase per cell, so a ramp never aliases.

## Rules

- **Pure functions of ctx.** Same ctx, same frame: no state between frames, no clocks.
  `ctx.beat_position` is bar-locked (`floor(beat_position) % ctx.beats_per_bar` is the beat in
  bar, 0 the one); prefer the ctx form of `sf.beat_in_bar(ctx)`, `sf.bar(ctx)` and
  `sf.phrase_phase(ctx, bars)`.
- **Ramps are for buildups.** Only buildups ramp on `section_elapsed_beats` and
  `beats_until_next`. Any loop may read the phrase time
  `ctx.section_elapsed_beats or ctx.autoloop_elapsed_beats` to keep a layer in step with its
  phasers, which count from the section start.
- **Palette slots only.** Colours come from `sf.palette()` (`sf.mix` of two slots is fine), and
  `sf.white()` only in overlays. Roles: primary is colour A, secondary colour B, cold the
  neutral, accent A's punch. Autolux plays the `_mono` variant (B and accent are A, cold is A
  held low) before a track's first drop and in every build and breakdown, `_drop` in drops.
- **Write for any rig.** Never a fixture count, a cell name or a fact about the room. Use groups
  (`movers`, `wash`, `strip`; accent groups such as `fx` are refused, they play specials),
  `sf.each` indices and counts, `sf.mirror`, `sf.members` and positions.
- **Points derive from `sf.focus` and `sf.position`**, never literal venue coordinates. Aim
  through `sf.phaser`, `sf.aim_offset` or `sf.aim_direction`; `sf.aim` takes a point built from
  those (a group's shared focus, say).
- **Movers.** Mirrored shapes through `sf.phaser`; mover dimmers may pulse; a Wild ballyhoo is at
  least 30 degrees.
- **Base loops**: peaks at or below 0.9 on every cell, no strobe.
- **Loudness and music features are amplitude only.** `sf.loudness_gain(ctx.normalized_rms_level)`,
  `ctx.onset`, `ctx.bass`, `ctx.mid` and `ctx.high` scale levels and reach, never colour, timing
  or pattern; each is nil until measured.
- **Seeded randomness only**: `sf.random(ctx, a, b, c)` with integers such as the `sf.each` index
  and a step count, never a cell id. Wrap the step on the loop length so the loop repeats.
- **Small.** At most about 80 lines, at most three comment lines saying why a technique is
  non-obvious, no other comments: encode intent in names.

## API summary

The context (`ctx`), in beats and 0..1 levels; nil where noted:

| Field | Meaning |
|---|---|
| `beat_position`, `bar_phase`, `bar_index`, `beats_per_bar`, `bpm` | The tracker's grid and tempo. |
| `autoloop_elapsed_beats` | Beats since this layer began (rotation entry or overlay begin). |
| `section_intent`, `section_elapsed_beats` | The active cue's intent and beats since its start (nil without a cue). |
| `next_intent`, `beats_until_next` | The pending cue's intent and beats until it starts (nil until known). |
| `drop_strength`, `next_drop_strength` | The active and pending drop's strength. |
| `seed` | The active cue's seed, or 0. |
| `normalized_rms_level`, `onset`, `bass`, `mid`, `high` | Loudness and the newest beat's features. |
| `overlay_length_beats` | An overlay's length (overlays only). |

Section and music helpers:

```lua
sf.random(ctx, a, b, c)        -- 0..1, a pure hash of ctx.seed and up to three integers.
sf.tension(ctx, plateau_beats) -- 0..1 smoothstep over the section's elapsed beats.
sf.finale(ctx, beats)          -- 0..1 over the last `beats` before the next cue; 0 when unknown.
sf.white()                     -- full white, for overlays only.
```

Aim and phasers. Points are metres in the MVR scene frame: X cross-stage, Y upstage, Z up.

```lua
sf.aim(cell, x, y, z) -> pan, tilt   -- nil, nil for a cell without a pan range
sf.focus(cell) -> x, y, z            -- the cell's MVR focus point
sf.aim_offset(cell, spread_degrees, elevation_degrees, beam) -> pan, tilt
-- Elevation turns the rest beam in its vertical plane, positive up; spread turns it about the
-- perpendicular in that plane, so a circle of offsets is a cone. Positive spread moves any throw
-- with a Y component towards +X. `beam` = {dx, dy, dz} is the rest beam, default focus - position.
sf.aim_direction(cell, dx, dy, dz) -> pan, tilt  -- the beam along a vector: parallel walls, beams up
sf.phaser(target, ctx, spec) -> effects
-- spec = { shape = "fan"|"cross"|"circle"|"figure8"|"tilt_wave"|"ballyhoo",
--   size = spread degrees (tilt_wave needs none), tilt_size = elevation degrees (default size),
--   measure = beats per cycle: 1, 2, 4, 8, 16 or 32 (nil holds a fan or cross still),
--   phase = -1..1 cycles spread across the selection; positive runs left to right,
--   wings = mirror partners share a rank and move outward (default true; false for tilt_wave),
--   centre_pan, centre_tilt = degrees added to every member,
--   at = {x, y, z} every beam rests on, or direction = {dx, dy, dz} (default each focus) }
-- Cycles count from the section start; change measure only where the elapsed beats are a
-- multiple of both measures (any bar line for measures up to 4).
```

Fixture vocabulary:

```lua
sf.gobo(target, slot) -- one Gobo1 preset per member with a gobo wheel. Slot 0 is the open slot,
                      -- 1.. the others in wheel order, wrapping, so any slot works on any rig.
```

`sf.colour` snaps to the nearest wheel slot on a wheel-only fixture, and a blend changes slot
half-way rather than landing on a third colour.

## Metadata

Base loops: line 1 is `-- 8-beat loop` or `-- 16-beat loop` (the tests read the loop length
from it), then exactly these three lines in any order, which the director uses to pair
contrasting looks. Both Autolux readiness and the Starfire tests reject a missing key or an
unknown value.

```
-- energy: 2
-- movement: sweep
-- texture: wash
```

- `energy`: `1` calm, `2` driving, `3` maximal. Atmospheric and breakdown stay at or below 2;
  drops are 2 or 3; a buildup's energy is the drop it leads into.
- `movement`: `still` (parked or barely drifting), `sweep` (continuous motion), `snap` (jumps
  between positions).
- `texture`: which roles carry the look: `wash`, `beam` (the movers), `pixel` (the strip),
  `mixed`.

Overlays carry the strobe and intensity header above instead.

## Adding a loop

Drop the file into its bank directory and run `cargo test -p starfire --test autoloop_replay`.
The tests discover it, replay it on the generic stage rig (the bank default
`tests/replays/<bank>.json`, unless `tests/replays/<bank>/<loop>.json` exists) and run the
per-bank semantic checks. Golden DMX digests guard the renderer pipeline, one loop per bank
(`tests/golden/<bank>/<loop>.sha256`); new loops need no golden. After an intended render
change, re-record the existing goldens with `STARFIRE_WRITE_GOLDEN=1`, never to turn a red
semantic check green. A loop the preferred table names (`PREFERRED_AUTOLOOPS` in
`autolux/lib/engine/effect_definitions.py`) also has its metadata row there.
