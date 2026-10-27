# Specials

One TOML file per special, `show/specials/<name>.toml`. A special is an accent Autolux fires on
top of the running loop when an intent starts: a rig group, a few abstract values, and how they
ease in and out on the musical clock. Loops never know specials exist; Starfire maps the values
onto whatever the group's GDTF profiles declare. Nothing here names a fixture model: a special
names a rig group (an MVR class such as `fx`), and any fixture patched into that group plays it.

## Format

The file reads like a lighting console's effect sheet: what it plays on, the preset it takes
its colour from, the effect's shape, and how playback eases it.

```toml
name = "cold_spin"
intents = ["drop"]
group = "fx"

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
- `group`: the rig group it plays on, or `all` for the whole rig.

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
- `cooldown_seconds` (optional): once the row fires, the policy keeps it out of selection this
  long, on top of the shared intense cooldown below. Absent means 0, no row cooldown. It stays
  in Autolux and never goes on the wire.

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
axis the special sets (colour, speed when above 0, pulse, pattern; energy always), and out of
its row cooldown. Within those
it plays each one before repeating any, then favours the least recently used, seeded like the
loops; strobe rows are not candidates and join whichever special is chosen. An intent with no
playable special releases the one running.

The room is a living room, so the `fx` group plays two tiers and stays dark through buildup:

- Textures (`drift`, `cold_drift`) on `atmospheric` and `breakdown`: low size and rate, no
  pulse, an 8-bar attack and 16-bar release, and a 240 s row cooldown, so a texture blooms in,
  fades out and cannot return for four minutes. They are calm, so they never use the drop
  strobe cooldown.
- Drop accents (`spin`, `cold_spin`): intense, so they share the drop strobe cooldown, and each
  also has a 90 s row cooldown. An entry with no playable row releases the running special;
  a drop accent replaces a running texture, and Starfire runs the texture's release. A soft
  drop, one whose arrival is known and below 0.9 of the track peak (the autoloop policy's
  grading), keeps its strobe burst but plays no intense special.

## Adding a special

Drop the file in this directory and run
`pytest autolux/tests/test_special_catalogue.py`. In Static mode the same values can be fired by
hand on any special-capable group.
