# Specials

One JSON file per special, `specials/<name>.json`. A special is an accent Autolux fires on top
of the running loop when an intent starts: a rig group, a few abstract values, and how they
ease in and out on the musical clock. Loops never know specials exist; Starfire maps the values
onto whatever the group's GDTF profiles declare. Nothing here names a fixture model: a special
names a rig group (an MVR class such as `fx`), and any fixture patched into that group plays it.

## Fields

```json
{
  "name": "drift",
  "intents": ["atmospheric"],
  "group": "fx",
  "colour_slot": "secondary",
  "energy": 0.2,
  "speed": 0.1,
  "attack_beats": 32,
  "release_beats": 32
}
```

- `name`: the file name without `.json`.
- `intents`: the intents it can start with, from `atmospheric`, `breakdown`, `buildup`, `drop`.
- `group`: the rig group it plays on, or `all` for the whole rig.
- `colour_slot`: a slot of the active palette (`primary`, `secondary`, `accent`, `cold`, `black`).
  Starfire resolves it, so a special follows the palette like the loops do. Starfire ignores an
  unknown slot, so the catalogue rejects one at load, naming the file.
- `energy`, `speed`: 0..1. Energy is how much light; speed is how fast the group moves over the
  physical range its profile declares (rotation, scan, motion).
- `pulse_beats` (optional): step or burst on the beat grid every N beats; absent means continuous.
- `attack_beats`, `release_beats`: ease-in when it starts and ease-out when Autolux clears or
  replaces it. Autolux never cuts a special; Starfire runs the release.
- `pattern` (optional): 0..1, a figure, gobo or clip index where the profile has one.

Values are sent as written. Starfire applies the intensity macro: it scales speed and gates and
caps pulse; energy is not scaled. Autolux only gates which specials may fire (below).

A strobe row has `name`, `intents`, `group` and `strobe_hz` instead, and goes out as the capped strobe accent; its length follows the strobe burst settings.
A strobe row is never selected: it fires on every eligible entry of its intents, alongside the
selected special. `strobe_burst` is the drop burst.

Unknown or missing keys, out-of-range values and a name that does not match the file are
rejected at startup, with the file and field named.

## Intense

A special is intense when it pulses, strobes, or its speed is above `INTENSE_SPEED` (0.6).
This is derived from the values and never declared. An entry that fires the strobe burst
and an intense special counts as one intense event on one cooldown, the drop strobe cooldown
setting (longer as intensity falls), and nothing intense fires below intensity 0.3. While the
cooldown runs neither the burst nor an intense special fires; a calm special may. Calm specials
are free.

## Selection

When an intent starts, Autolux picks among that intent's specials that the rig can play:
the group must be reported by Starfire at login with fixtures in it, and able to express every
axis the special sets (colour, speed when above 0, pulse, pattern; energy always). Within those
it plays each one before repeating any, then favours the least recently used, seeded like the
loops; strobe rows are not candidates and join whichever special is chosen. An intent with no
playable special releases the one running.

`breakdown` has none on purpose: the room falls back to the loop alone, and the next buildup's
rise reads against that.

## Adding a special

Drop the file in this directory and run
`pytest autolux/tests/test_special_catalogue.py`. In Static mode the same values can be fired by
hand on any special-capable group.
