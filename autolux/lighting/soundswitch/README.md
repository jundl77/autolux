# Home section loops

Four 16-bar SoundSwitch timelines share a blue/violet palette and emphasize
contrast between musical sections. They target the existing **Home** venue.

| Intent | Sections | Loop | MIDI note | Native file |
| --- | --- | --- | --- | --- |
| Atmospheric | intro, altintro, outro, altoutro | ATM Blue Tide · 2A | 35 | `SSAutoLoop33.ssfile` |
| Breakdown | breakdown, bridge, cooldown | BREAK Violet Space · 2F | 40 | `SSAutoLoop38.ssfile` |
| Buildup | buildup | BUILD Rising Current · 1A | 27 | `SSAutoLoop1.ssfile` |
| Drop | drop | DROP Blue Impact · 1F | 32 | `SSAutoLoop6.ssfile` |

Atmospheric uses dim light and slow movement. Breakdown holds a darker, still look.
Buildup repeats moderate beat-synced fades without a phrase-length intensity ramp.
Drop uses brighter, gentle pulses and more movement after a one-second MIDI
strobe on section entry. Held sections do not retrigger the strobe.
Beat sync follows tempo; rising musical energy does not imply
rising BPM, and these loops do not automatically follow an energy envelope.

Autolux selects a loop only on a committed section change. In SoundSwitch, disable
**Randomise Playback Order**, enable **Override Scripted Tracks**, and activate
**Repeat Autoloop** with **Infinite Autoloop Repeat** selected in preferences.
Repeat is a toggle: do not toggle it blindly on every program start.
OS2L owns playback start/stop. MIDI selects looks and levels; its Play/Pause
toggle is used only when OS2L is disabled.

MIDI bindings belong to a specific input port. Use **loopMIDI Port** in SoundSwitch
(shown as **loopMIDI Port 1** by Autolux), MIDI channel 1. A blue mapping border
alone does not confirm that the current port is bound. The `recordable/` files
save this Home project's native MIDI settings, including the four loops above,
Play/Pause (note 9), strobe (note 12), and Repeat Autoloop (note 91).
Autoloop, scripted-track and group 1–4 intensities use CC 3–8 respectively.

These native files require the matching Home venue and its fixture identifiers;
they are not standalone fixture presets. To restore a timeline, close SoundSwitch,
back up the project, and copy its native file into the matching `.ssproj` directory.
Names and slot assignments are stored separately by SoundSwitch. Keep a full
project backup when moving the show to another computer.
Restore `recordable/` only into the matching Home project while SoundSwitch is
closed; these files replace its MIDI settings.
