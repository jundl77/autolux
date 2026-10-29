# Autoloops

One Lua file per look, `show/autoloops/<bank>/<loop>.lua`. The API is
`starfire/src/lua/lighting.lua` plus the native bindings in `starfire/src/script.rs`.

- **Base banks** are the intents `atmospheric`, `breakdown`, `buildup` and `drop`. A section
  cue plays a rotation of base loops, one at a time, each for a whole number of bars.
- **Overlay banks** are `hit`, `fill`, `riser`, `dip` and `accent`. A cue schedules them above
  its base, once or every few bars, each with a begin, a length and an attack and release
  envelope.

`standby/white` is the exception: Autolux shows it while the input is digitally
silent (the player stopped). It is outside the rotation, required by readiness, and
deliberately static: no beat or loudness response, movers parked dark, `sf.white()` held low
so it never depends on a palette's name.

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

- **Base.** One loop at a time; steps change on downbeats.
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

- **Regime U, the drop unknown.** `drive = 0.25 + 0.65 * tension`, with tension `e / (e + 48)`
  of section time: 0.3 in bar 1, 0.51 at bar 8, 0.62 at bar 16 (twice bar 1), 0.72 at bar 32,
  still rising, so an uncued build climbs from its first bar and never restarts.
- **Regime K, a drop cued.** `drive` hands over continuously to 1 over the last 16 beats before
  the dip (`drive + (1 - drive) * finale`); a cue that arrives with fewer than 16 beats to go
  hands over across what is left (`build.span`, `sf.approach`), never in a jump. Counts move one step
  per beat (`math.ceil(build.left)`) and land on the dip, not the drop.
- **A cued non-drop** eases `drive` down by half instead of building a finale.
- **Steps land on the one:** compare thresholds with `build.step`, the tension at the last
  downbeat, never with the continuous tension.
- **Stillness and tightening into the last bar:** motion collapses, rates are at their fastest,
  the level is at its peak. The blackout itself comes from the cue's `dip` overlay, never from
  the loop.
- **Never scaled by loudness:** a build's RMS falls as the music high-passes, so a build that
  followed it would dim exactly as the music climbed. Onset may scale how hard a pulse hits.
- **Groups enter one at a time and beams lift** as the build climbs; phaser rates climb the
  ladder in `build.measure` (8, 4, 2 beats, moving only on 8-beat lines so a cycle never jumps).

Each buildup is one idea that drives every layer:

| Loop | Idea |
|---|---|
| `countdown` | Counting: the strip is a meter filling from the centre to half its length with the build's tension (stepping on downbeats); over a cued drop's last eight beats the count closes the rest from both ends one step per beat, full on the last beat before the dip; the beams lift. |
| `tighten` | Converging: the fan closes onto the movers' shared point while the washes hold back, so the closed beam point is the brightest thing in the room. |
| `stutter` | Subdividing: the roll halves on downbeats (a bar, half a bar, a beat, half a beat) and its gaps fill in until it is a wall of light. |
| `stack` | Layering: strip first, washes from a third of the build, movers from two thirds, the beams lifting with it; the last bar locks solid. |
| `inhale` | Holding: two-bar breaths that deepen; once the drop is cued everything freezes at the top of the inhale. |
| `release` | Letting go: a dim, motionless share of the drive until half-way, then the beams open into a sweep and the dot into a chase (energy 2, so Calm can play it). |

### The rest of the catalogue

- **Drops** pair a wash groove with a mover phaser and a strip chase. The beams are the
  production (mover floor 0.6, so a drop is never dimmer than its build) and carry colour B; in
  `hammer` and `crossfire` the washes swap A and B every two bars with the beams opposite, so
  the drop changes colour after a mono build. Looks alternate on section time and snap in the
  dark, so a drop opens on its wide look whatever bar it starts on. `kick`, `hammer`, `crossfire`, `piston_kick`, `rolling`
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
| `hit` | `white` (washes, strip and beams full white, the beams through the open gobo where they already point), `colour_bump` (Calm: the palette's punch colour), `strobe` (Wild: white strobe on every group) | HTP |
| `fill` | `strip_wipe` and `mover_flick` (bar 8), `turnaround` (bar 16, in the punch colour: washes and beams roll eighths, the strip doubles to sixteenths, lands on a dark eighth) | LTP |
| `riser` | `white_rise` (Balanced: washes and beams rise in white, no position), `strobe_ramp` (Wild: white and strobe over the last eight beats only, so the build's finale shows first), `white_sweep` (beams lift in white and land back on the focus over the last bar, washes swell to 0.6), `strip_fill` (Calm: the strip closes from the ends in the punch colour) | HTP |
| `dip` | `last_beat` (every group dark), `fade` (a low glow for breakdown entry) | LTP |
| `accent` | `sparkle` (seeded strip sparkle on the off-beat eighths), `wash_roll` (a quarter of the washes per off-beat, a lap in eight beats), `mover_shapes` (figure-eights through a gobo, swapping A and B on the two-bar line where `hammer` and `crossfire` swap their washes) | HTP, but `mover_shapes` LTP |

An overlay's first line is free text saying what it does, then exactly these four lines in
any order:

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
- **Strobe rates are beat divisions of `ctx.bpm`**, stepped on beat lines:
  `sf.beat_strobe_hz(ctx, per_beat)` picks the fastest division up to `per_beat` flashes a beat
  that `ctx.strobe_max_hz` (the cap at this intensity, 0 where strobing is off) lets through. A strobing
  overlay starts on its own beat line whatever the base's level, and its gate covers the
  whole cell, base included.

## Rules

- **Pure functions of ctx.** Same ctx, same frame, no clocks. A render keeps no state between
  calls, neither in globals nor in chunk-level locals (upvalues): one compiled VM serves the
  base and every overlay layer naming the loop and survives reselection, so a counter, cache
  or accumulator leaks between layers and sections. Compute everything from ctx and vary with
  `sf.random(ctx, ...)`. Globals are frozen after load, and Login renders each loop twice on
  one frame and refuses it if the two differ, but that only catches state that changes what a
  frame shows; this rule is the whole contract. Constants computed at load are fine.
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
  `sf.each` indices and counts, `sf.mirror`, `sf.members` and positions. Widths along a row are
  a share of its cells with a one-cell minimum (`sf.pixels(share, count)`), never a cell count,
  and positions in a row come from `sf.from_centre`, which never divides by `count - 1`. Prefer
  group-wide effects and colour only the members that differ, so a 400-cell rig stays inside the
  512-effect cap.
- **Points derive from `sf.focus` and `sf.position`**, never literal venue coordinates. Aim
  through `sf.phaser`, `sf.aim_offset` or `sf.aim_direction`; `sf.aim` takes a point built from
  those (a group's shared focus, say).
- **Movers.** Mirrored shapes through `sf.phaser`; mover dimmers may pulse; a Wild ballyhoo is at
  least 30 degrees. A snap moves only in the dark: `sf.snap` changes the look on the off-beat
  with the mover dimmers at 0 for that quarter beat.
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
| `autoloop_elapsed_beats` | Beats since this layer began (step or overlay begin). |
| `section_intent`, `section_elapsed_beats` | The active cue's intent and beats since its start; for a manual selection the intent is nil and the beats count from the bar line at or before the selection. |
| `next_intent`, `beats_until_next` | The pending cue's intent and beats until it starts (nil until known). |
| `next_known_at` | The section time at which the pending cue arrived, where tension bends towards it. |
| `strobe_max_hz` | The strobe rate the engine lets through at this intensity (0 where strobing is off). |
| `beats_until_dip` | Beats until the pending cue's dip row begins (nil without one). |
| `drop_strength`, `next_drop_strength` | The active and pending drop's strength. |
| `seed` | The active cue's seed, or 0. |
| `normalized_rms_level`, `onset`, `bass`, `mid`, `high` | Loudness and the newest beat's features. |
| `overlay_duration_beats` | An overlay's duration (overlays only). |

A loop is a file returning a render function of ctx; this one is complete:

```lua
-- 8-beat loop
-- energy: 2
-- movement: sweep
-- texture: wash
local palette = sf.palette()
return { render = function(ctx)
    local level = 0.9 * sf.on_beat(ctx, 0.4) * sf.loudness_gain(ctx.normalized_rms_level)
    return sf.scene { sf.colour(sf.group("wash"), palette.primary), sf.dimmer(sf.group("wash"), level),
        sf.phaser(sf.group("movers"), ctx, { shape = "fan", size = 20, measure = 8 }) }
end }
```

Building blocks (all of `lighting.lua`):

```lua
sf.scene{...}                  -- flattens effects and lists of effects into one frame.
sf.dimmer(target, level), sf.colour(target, rgb), sf.pan_tilt(target, pan, tilt)
sf.group(name), sf.fixture(cell), sf.all()   -- targets; sf.each(target, fn(member, index, count))
sf.members(target), sf.mirror(target), sf.position(cell) -- cells, mirror pairs, metres
sf.on_beat(ctx, width, period) -- 1 on the beat decaying to 0 over `width` beats, every `period`.
sf.osc.sine/triangle(beats, period, low, high, offset), sf.osc.pulse(..., duty, offset)
sf.lerp(a, b, t), sf.mix(rgb_a, rgb_b, t), sf.rgb(r, g, b)
sf.energy(ctx), sf.loudness_gain(rms) -- RMS (a typical level before one is measured) and its gain.
sf.beat_in_bar(ctx), sf.bar(ctx), sf.phrase_phase(ctx, bars) -- the bar grid, 0..1 in a phrase.
```

Section and music helpers:

```lua
sf.random(ctx, i, j, k)        -- 0..1, a pure hash of ctx.seed and up to three integers.
sf.section_beats(ctx)          -- phrase time: beats since the section start, else the layer's.
sf.tension(ctx, half_beats)    -- 0..1 build progress (e / (e + 48) until the next change is
                               -- known, then linear to it), and its value at the last downbeat.
sf.finale(ctx, beats, from)    -- 0..1 over the last `beats` before the next cue (0 when none);
                               -- with `from`, from + (1 - from) x that. The second value holds
                               -- each bar at the level its end reaches.
sf.onset(ctx), sf.band(ctx, name) -- the newest beat's onset and band level, a mid level until measured.
sf.beat_strobe_hz(ctx, per_beat) -- the fastest beat-division strobe rate the engine lets through.
sf.white()                     -- full white, for overlays only.
```

Show helpers, shared by the banks (the show-helpers section of `lighting.lua`):

```lua
sf.build(ctx) -> { drive, finale, tension, step, stage, measure, left, span }
-- A buildup's shared state: drive 0..1 (regime U 0.25 + 0.65 x tension, handed over to 1 by a
-- cued drop's finale, eased down before a cued non-drop), finale 0..1, tension and step (the
-- tension at the last downbeat), stage 0, 1, 2 (from step at a third and a half), measure (the
-- 8, 4, 2-beat rate ladder by stage, moving on 8-beat lines), left = beats until a cued drop's
-- dip begins (nil while no drop is cued), span = the beats the finale runs over.
sf.approach(build, beats)      -- 0..1 over the last `beats` before the dip, over less after a
                               -- late cue, so nothing timed to the drop jumps; 0 uncued.
sf.drop_within(build, beats)   -- a drop is cued and its dip is at most `beats` away.
sf.drop_levels(ctx, floor_quiet, floor_loud) -> gain, floor, energy
                               -- a drop's loudness gain and its floor between the two by energy.
sf.snap(ctx, period)           -- look 0/1 changing on the off-beat before each period line, and
                               -- `dark` for the quarter beat after, to move heads in the dark.
sf.snap_look(ctx, period, looks, reach) -> spec, dark, index
                               -- sf.snap between two {shape, size, centre_tilt} looks, scaled by reach.
sf.colours_swapped(ctx)        -- alternates on a fixed 8-beat line of section time, so drops and
                               -- accents swap A and B together.
sf.pixels(share, count)        -- a width along a row: share x count cells, at least one.
sf.from_centre(index, count)   -- 0 at the centre of an sf.each row, 1 at either end, 0 for one.
sf.is_middle(index, count)     -- the middle cell of a row, or the middle two.
sf.mirrored_spot(index, count, at, share) -- 1 where from_centre is `at`, 0 `share` of the row away.
sf.chase(index, count, head, tail_share) -> level, cells_behind -- a tail behind `head` (0..1 a lap).
sf.shared_focus(target)        -- {x, y, z}, the mean of the members' focus points.
sf.overlay_progress(ctx)       -- an overlay's progress 0..1 and the beats it has left.
sf.white_strobe(level, hz)     -- wash, strip and movers in white at `level`, strobing at `hz`;
                               -- overlays only.
```

Aim and phasers. Points are metres in the MVR scene frame: X cross-stage, Y upstage, Z up.

```lua
sf.aim(cell, x, y, z) -> pan, tilt   -- nil, nil for a cell without a pan range
sf.focus(cell) -> x, y, z            -- the cell's MVR focus point
sf.aim_offset(cell, spread_degrees, elevation_degrees, beam) -> pan, tilt
-- Elevation turns the rest beam in its vertical plane, positive up, and applies first; a plumb
-- beam turns about X. Spread turns it about the perpendicular in that plane, so a circle of
-- offsets is a cone. Positive spread moves any throw with a Y component towards +X, and a pure
-- cross-stage throw clockwise seen from above. `beam` = {dx, dy, dz} is the rest beam, default
-- focus - position.
sf.aim_direction(cell, dx, dy, dz) -> pan, tilt  -- the beam along a vector: parallel walls, beams up
sf.phaser(target, ctx, spec) -> effects
-- spec = { shape = "fan"|"cross"|"circle"|"figure8"|"tilt_wave"|"ballyhoo",
--   size = spread degrees (tilt_wave needs none), tilt_size = elevation degrees (default size),
--   measure = beats per cycle: 1, 2, 4, 8, 16 or 32 (nil holds a fan or cross still),
--   phase = -1..1 cycles spread across the selection; positive runs left to right,
--   wings = mirror partners share a rank and move outward (default true; false for tilt_wave),
--   centre_pan, centre_tilt = degrees added to every member,
--   at = {x, y, z} every beam rests on, or direction = {dx, dy, dz} (default each focus),
--   spin = sf.spin spec: one member's look turns a full circle and back (below) }
sf.pan_reach(cell) -> clockwise, anticlockwise -- degrees of heading the pan can turn from the
-- focus aim (clockwise seen from above) before an end stop; nil, nil without a pan range
sf.spin(target, ctx, spec) -> cell, degrees    -- nil between spins
-- spec = { measure = 4, 8 or 16 beats, every = period in beats (default 64: 16 bars),
--   at = beats into each period it starts (default 0) }. One member whose pan reaches a full
-- circle one way turns round and back, eased; sf.random picks it, and the way, per period.
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

Overlays carry the four-line header above instead. Starfire tells the two apart by these
headers alone, not by bank: `energy` marks a base loop, `strobe` and `intensity` an overlay,
and a file with neither fails to load. `standby/white` carries a base header for that reason.

## Adding a loop

Drop the file into its bank directory and run `cargo test -p starfire --test autoloop_replay`.
The tests discover it, replay it on the generic stage rig (the bank default
`tests/replays/<bank>.json`, unless `tests/replays/<bank>/<loop>.json` exists; an overlay bank's
replay plays the overlay on a fixed base) and run the semantic checks:

- base loops peak at or below 0.9, never strobe and pulse no faster than half-beat steps;
- buildups never restart without a cued drop (`buildup.json`), rise into a cued drop and leave
  the dip dark (`buildup_finale.json`); `countdown` fills one step a beat over the last eight;
- beat features and loudness change levels and reach only (loudness never for buildups);
- an overlay strobes exactly when its header says so and is gone after its length and release;
- the same rules hold on a bigger generic rig (eight movers, a 40-cell strip), every loop stays
  inside the 512-effect cap on 400 washes and on 60 eight-cell bars, and a fan spreads in stage
  order on a shuffled patch;
- every loop's worst frame stays lean in Lua instructions (the suite prints the table with
  `-- --nocapture`).

Golden DMX digests guard the renderer pipeline, one loop per bank
(`tests/golden/<bank>/<loop>.sha256`); new loops need no golden. After an intended render
change, re-record the existing goldens with `STARFIRE_WRITE_GOLDEN=1`, never to turn a red
semantic check green. A loop the preferred table names (`PREFERRED_AUTOLOOPS` in
`autolux/lib/engine/effect_definitions.py`) also has its metadata row there.
