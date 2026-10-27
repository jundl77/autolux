# Specials

One TOML file per special, `show/specials/<name>.toml`. A special is an accent Autolux fires on
top of the running loop when an intent starts: a rig group, a few abstract values, and how they
ease in and out on the musical clock. Loops never know specials exist; Starfire maps the values
onto whatever the group's GDTF profiles declare. Nothing here names a fixture model: a special
names a rig group (an MVR class such as `fx`) or a role (the MVR layer a group sits in, such as
`accent`), and any fixture patched into a matching group plays it.

## Format

The file reads like a lighting console's effect sheet: what it plays on, the preset it takes
its colour from, the effect's shape, and how playback eases it.

```toml
name = "cold_spin"
intents = ["drop"]
role = "accent"

[preset]
colour = "cold"

[effect]
rate = 0.8
size = 0.8
form = { pulse_beats = 1 }

[playback]
attack_beats = 0
release_beats = 4
cooldown_seconds = 90
```

Selection (top level):

- `name`: the file name without `.toml`.
- `intents`: the intents it can start with, from `atmospheric`, `breakdown`, `buildup`, `drop`.
- `group`: the rig group it plays on, or `all` for the whole rig; or
- `role`: `accent` or `loop`, instead of `group`: the row plays on every group of that role whose
  profiles can express it (below), so a new accent group inherits it. Each such group is a
  separate candidate and rests on its own. A strobe row always names a group.
- `zone` (optional, role rows only): `house_left`, `centre` or `house_right`, seen from the
  audience.
- `depth` (optional, role rows only): `downstage`, `centre` or `upstage`.
  Both pick a column or row of the nine-area stage grid: the rig's bounding box cut into thirds
  on each axis, derived from the fixtures' MVR positions, never from names. The row plays only
  on groups whose members all lie in one area, and that area is in the zone and depth given.

`[preset]`:

- `colour`: a slot of the active palette (`primary`, `secondary`, `accent`, `cold`, `black`).
  Starfire resolves it, so a special follows the palette like the loops do. Starfire ignores an
  unknown slot, so the catalogue rejects one at load, naming the file.

`[effect]`:

- `rate`: 0..1, how fast the group moves over the physical range its profile declares
  (rotation, scan, motion).
- `size`: 0..1, how much light.
- `form` (optional): `{ pulse_beats = N }` steps or bursts on the beat grid every N beats;
  absent means continuous.
- `pattern` (optional): 0..1, a figure, gobo or clip index where the profile has one.

`[playback]`:

- `attack_beats`, `release_beats`: ease-in when it starts and ease-out when Autolux clears or
  replaces it. Autolux never cuts a special; Starfire runs the release.
- `cooldown_seconds` (optional): how long the row's group rests after this row fires. While a
  group rests, no row targeting it is selectable, whichever row fired: the cooldown belongs to
  the fixtures, so a group counts as one FX however many recipes it has. It runs on top of the
  shared intense cooldown below. Absent means 0, the group is free at once. It stays in Autolux
  and never goes on the wire.

On the wire these are `colour_slot`, `speed`, `energy`, `pulse_beats`, `pattern`,
`attack_beats` and `release_beats`; `FILE_FIELDS` in `autolux/lighting/special_catalogue.py`
is the only place the two vocabularies meet.

Values are sent as written. Starfire applies the intensity macro: it scales speed and gates and
caps pulse; energy is not scaled. Autolux only gates which specials may fire (below).

A strobe row has only the selection keys and `[effect] form = { strobe_hz = 8.0 }`, and goes
out as the capped strobe accent; its length follows the strobe burst settings.
A strobe row is never selected: it fires on every eligible entry of its intents, alongside the
selected special. `strobe_burst` is the drop burst.

Unknown or missing keys, out-of-range values and a name that does not match the file are
rejected at startup, with the file and the key's path (such as `effect.size`) named.

## Intense

A special is intense when it pulses, strobes, or its speed is above `INTENSE_SPEED` (0.6).
This is derived from the values and never declared. An entry that fires the strobe burst
and an intense special counts as one intense event on one cooldown, the drop strobe cooldown
setting (halved at wild), and nothing intense fires at or below intensity 0.3. While the
cooldown runs neither the burst nor an intense special fires; a calm special may. Calm specials
are free.

## Selection

When an intent starts, Autolux picks among that intent's specials that the rig can play:
the group must be reported by Starfire at login with fixtures in it, and able to express every
axis the special sets (colour, speed when above 0, pulse, pattern; energy always), and not
resting after its last special. Within those
it plays each one before repeating any, then favours the least recently used, seeded like the
loops; strobe rows are not candidates and join whichever special is chosen. An intent with no
playable special releases the one running. When the pick lands on an accent and several accent
groups are ready, their MVR positions choose the group: house left-most first, then each time the
ready group furthest in X from the last accent (equal reach: the one that waited longest), so
accents alternate across the room instead of following the seed.

The room is a living room, so the accent group `fx` plays two tiers and stays dark through
buildup (the rows select `role = "accent"`, so a second accent group able to express them, such
as a laser that rotates and colours, plays them too):

- Textures (`drift`, `cold_drift`) on `atmospheric` and `breakdown`: low size and rate, no
  pulse, an 8-bar attack and 16-bar release, and a 240 s cooldown, so after a texture blooms in
  and fades out `fx` plays nothing, texture or accent, for four minutes; a drop in that window
  still gets its strobe burst. They are calm, so they never use the drop
  strobe cooldown.
- Drop accents (`spin`, `cold_spin`): intense, so they share the drop strobe cooldown, and each
  rests `fx` for 90 s. An entry with no playable row releases the running special;
  a drop accent replaces a running texture, and Starfire runs the texture's release. A soft
  drop, one whose arrival is known and below 0.9 of the track peak (the autoloop policy's
  grading), keeps its strobe burst but plays no intense special.

## Venue

`venue.toml` is not a special; it holds the room's limits across all of them:

```toml
[venue]
max_accent_events_per_minute = 2
```

- `max_accent_events_per_minute` (default 2, at least 1): specials fired on accent groups in any
  sliding 60 s, counted across every accent group. At the cap no accent row is selectable until
  the oldest of those events is a minute old; loop-group rows and strobe rows are not counted or
  capped. The FX Effects block shows it as the bar `venue · accents n/cap`.

## Adding a special

Drop the file in this directory and run
`pytest autolux/tests/test_special_catalogue.py`. In Static mode the same values can be fired by
hand on any special-capable group.
