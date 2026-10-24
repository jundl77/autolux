# Home section loops

Four 16-bar SoundSwitch timelines share a blue/violet palette and emphasize
contrast between musical sections. They target the existing **Home** venue.

| Intent | Sections | Loop | MIDI note | Native file |
| --- | --- | --- | --- | --- |
| Atmospheric | intro, altintro, outro, altoutro | ATM Blue Tide · 2A | 35 | `SSAutoLoop33.ssfile` |
| Breakdown | breakdown, bridge, cooldown | BREAK Violet Space · 2F | 40 | `SSAutoLoop38.ssfile` |
| Buildup | buildup | BUILD Rising Current · 1A | 27 | `SSAutoLoop1.ssfile` |
| Drop | drop | DROP Blue Impact · 1F | 32 | `SSAutoLoop6.ssfile` |

Atmospheric uses slow pulses and movement. Breakdown holds a dim, still look.
Buildup raises master intensity across the phrase. Drop adds brighter,
beat-driven accents and more movement. The separate MIDI strobe remains available.

Autolux selects a loop only on a committed section change. In SoundSwitch, disable
**Randomise Playback Order**, enable **Override Scripted Tracks**, and activate
**Repeat Autoloop** with **Infinite Autoloop Repeat** selected in preferences.
Repeat is a toggle: do not toggle it blindly on every program start.

These native files require the matching Home venue and its fixture identifiers;
they are not standalone fixture presets. To restore a timeline, close SoundSwitch,
back up the project, and copy its native file into the matching `.ssproj` directory.
Names and slot assignments are stored separately by SoundSwitch. Keep a full
project backup when moving the show to another computer.
