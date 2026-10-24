# loopMIDI

Install `loopMIDISetup.exe`, then create a virtual port named `loopMIDI Port`.
Select that port as a MIDI input in SoundSwitch and configure its lighting mappings.
Leave SoundSwitch's MIDI mapping mode before running Autolux.

Production starts loopMIDI when needed. It is a shared utility and remains open
when the show stops. Use `uv run autolux list` to check device and port names.
