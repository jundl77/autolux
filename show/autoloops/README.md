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

Every buildup reads one shared state, `sf.build(ctx)` (below), and implements both regimes:

- **Regime U, the drop unknown.** `drive` rises with the build's tension (an ease-in: held back
  early, climbing late) to a 0.7 plateau, so an uncued build never peaks early or restarts.
- **Regime K, a drop cued.** `drive` hands over continuously to 1 over the last 16 beats before
  the dip, however late the cue arrives (`drive + (1 - drive) * finale`). Counts move one step
  per beat (`math.ceil(build.left)`) and land on the dip, not the drop.
- **A cued non-drop** eases `drive` down by half instead of building a finale.
- **Steps land on the one:** compare thresholds with `build.step`, the tension at the last
  downbeat, never with the continuous tension.
- **Stillness and tightening into the last bar:** motion collapses, rates are at their fastest,
  the level is at its peak. The blackout itself comes from the cue's `dip` overlay, never from
  the loop.
- **Never scaled by loudness:** a build's RMS falls as the music high-passes, so a build that
  followed it would dim exactly as the music climbed. Onset may scale how hard a pulse hits.
- **Groups enter one at a time and beams lift** as the build climbs.

Each buildup is one idea that drives every layer:

| Loop | Idea |
|---|---|
| `countdown` | Counting: the strip fills from the centre one step per downbeat, then counts back one step per beat over the last eight; the beams lift. |
| `tighten` | Converging: the fan closes onto the movers' shared point while the washes hold back, so the closed beam point is the brightest thing in the room. |
| `stutter` | Subdividing: the roll halves on downbeats (a bar, half a bar, a beat, half a beat) and its gaps fill in until it is a wall of light. |
| `stack` | Layering: strip first, washes from a third of the build, movers from two thirds, the beams lifting with it; the last bar locks solid. |
| `inhale` | Holding: two-bar breaths that deepen; once the drop is cued everything freezes at the top of the inhale. |
| `release` | Letting go: dim and motionless until half-way, then the beams open into a sweep and the dot into a chase. |

### The rest of the catalogue

- **Drops** pair a wash groove with a mover phaser and a strip chase, washes on colour A and,
  in at least half of them, the movers on colour B, so the drop changes colour after a mono
  build. Looks alternate on section time, so a drop opens on its wide look whatever bar it
  starts on; mover dimmers pulse. `kick`, `hammer`, `crossfire`, `piston_kick`, `rolling`
  (directional chase, circles), `wide_fan` (slow fan, centre-out waves), `ballyhoo` (Wild:
  30-degree ballyhoo, seeded sparkle) and `wall_split` (outer members in colour B).
- **Breakdowns** are mono and dark-first, at most one swell a bar: `heartbeat`, `afterglow`,
  `cathedral`, `drift_apart`, `gobo_breakup` (a slow gobo circle in haze) and `one_pixel`
  (negative space: one breathing cell).
- **Atmospherics** are two colours and slow: `amber_drift`, `boiler_room`, `cercle`, `dawn`,
  `fog`, `orbit` (slow circles about the focus) and `tilt_tide` (a tide crossing the room).

## Overlays

| Bank | Loops | Intensity |
|---|---|---|
| `hit` | `blinder` (washes and strip white), `beam_punch` (the beams white through the open gobo, where they already point), `colour_bump` (Calm: the palette's punch colour), `strobe` (Wild: white strobe on every group) | HTP |
| `fill` | `strip_wipe` and `mover_flick` (bar 8), `turnaround` (bar 16: every group rolls, lands on a dark eighth) | LTP |
| `riser` | `strobe_ramp` (Wild: strobing white rise), `white_sweep` (beams lift, washes and beams whiten), `strip_fill` (the strip closes from the ends) | HTP |
| `dip` | `last_beat` (every group dark), `fade` (a low glow for breakdown entry) | LTP |
| `accent` | `sparkle` (seeded strip sparkle on the off-beat eighths), `wash_roll` (one wash per off-beat eighth), `mover_shapes` (figure-eights through a gobo) | HTP, but `mover_shapes` LTP |

An overlay's first line is free text saying what it does, then exactly these four lines in any
order:

```
-- strobe: yes|no
-- intensity: htp|ltp
-- movement: still|sweep|snap
-- texture: wash|beam|pixel|mixed
```

- `strobe: yes` marks a strobing overlay; the director's venue gate counts it as an intense event.
- `intensity: htp` takes the higher of the look below and the overlay's own level (hits,
  risers, accents that add to the groove); `ltp` replaces the look below on the groups it sets
  (fills, dips, accents that take the movers over).
- `movement` and `texture` name the layer the overlay plays on, with the base loops' values, so
  the director keeps an accent or riser off a base loop whose lead layer it would fight.
- From the moment it fires, an overlay owns pan and tilt, strobe, gobo and wheel colour at its
  envelope level; only RGB colour under HTP follows the brightness share. So:
  - an overlay sets position only when it means to move the heads: a hit never does, or the
    heads would swing in light on the one;
  - an overlay that moves the heads gets at least half a beat of attack and release.
- **Hits are full on frame 0** (their attack is 0) and decay over their length.
- **Risers peak at 1.0** (the base loops' 0.9 is their headroom) and end where the dip starts.
- An overlay sets only the groups it is about; everything else shows the base.
- **Strobe rates are beat divisions of `ctx.bpm`** (`hz = bpm / 60 * division`), stepped on
  beat lines and never above the 10 Hz cap: pick the fastest division under it. A strobing
  overlay starts on its own beat line whatever the base's level, and its gate covers the
  whole cell, base included.

## Rules

- **Pure functions of ctx.** Same ctx, same frame: no state between frames, no clocks.
  `ctx.beat_position` is bar-locked (`floor(beat_position) % ctx.beats_per_bar` is the beat in
  bar, 0 the one); prefer the ctx form of `sf.beat_in_bar(ctx)`, `sf.bar(ctx)` and
  `sf.phrase_phase(ctx, bars)`.
- **Ramps are for buildups.** Only buildups ramp, through `sf.build(ctx)`. Any loop may read
  the phrase time `sf.section_beats(ctx)` to keep a layer in step with its phasers, which count
  from the section start; key alternating looks off it so a section opens on its first look.
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
- **Base loops**: peaks at or below 0.9 on every cell, no strobe, and no pulse faster than
  half-beat steps; anything faster reads as a strobe, and strobing is overlay-only.
- **Loudness and music features are amplitude only.** `sf.loudness_gain(ctx.normalized_rms_level)`,
  `sf.onset(ctx)` and `sf.band(ctx, "bass"|"mid"|"high")` scale levels and reach, never colour,
  timing, position or pattern. Buildups never use loudness (see above).
- **Seeded randomness only**: `sf.random(ctx, i, j)` with integers such as the `sf.each` index
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
| `beats_until_dip` | Beats until the pending cue's dip begins (nil without one). |
| `drop_strength`, `next_drop_strength` | The active and pending drop's strength. |
| `seed` | The active cue's seed, or 0. |
| `normalized_rms_level`, `onset`, `bass`, `mid`, `high` | Loudness and the newest beat's features. |
| `overlay_length_beats` | An overlay's length (overlays only). |

Section and music helpers:

```lua
sf.random(ctx, i, j)           -- 0..1, a pure hash of ctx.seed and two integers.
sf.tension(ctx, beats)         -- 0..1 build progress, and its value at the last downbeat.
sf.finale(ctx, beats)          -- 0..1 over the last `beats` before the next cue; 0 when unknown.
sf.onset(ctx), sf.band(ctx, name) -- the newest beat's onset and band level, a mid level until measured.
sf.white()                     -- full white, for overlays only.
```

Show helpers, shared by the banks (the W6 block in `lighting.lua`):

```lua
sf.build(ctx) -> { drive, finale, tension, step, left }
-- A buildup's shared state: drive 0..1 (regime U to 0.7, handed over to 1 by a cued drop's
-- finale, eased down before a cued non-drop), finale 0..1, tension and step (the tension at the
-- last downbeat), left = beats until a cued drop's dip begins (nil while no drop is cued).
sf.section_beats(ctx)          -- phrase time: beats since the section start, else the layer's.
sf.from_centre(index, count)   -- 0 at the centre of an sf.each row, 1 at either end.
sf.shared_focus(target)        -- {x, y, z}, the mean of the members' focus points.
sf.overlay_progress(ctx)       -- an overlay's progress 0..1 and the beats it has left.
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

Overlays carry the four-line header above instead.

## Adding a loop

Drop the file into its bank directory and run `cargo test -p starfire --test autoloop_replay`.
The tests discover it, replay it on the generic stage rig (the bank default
`tests/replays/<bank>.json`, unless `tests/replays/<bank>/<loop>.json` exists) and run the
per-bank semantic checks. Golden DMX digests guard the renderer pipeline, one loop per bank
(`tests/golden/<bank>/<loop>.sha256`); new loops need no golden. After an intended render
change, re-record the existing goldens with `STARFIRE_WRITE_GOLDEN=1`, never to turn a red
semantic check green. A loop the preferred table names (`PREFERRED_AUTOLOOPS` in
`autolux/lib/engine/effect_definitions.py`) also has its metadata row there.
