# Sequence reference

These localhost Art-Net captures contain two 64-beat cycles of each authored
SoundSwitch loop at 120 and 140 BPM. No audio or physical lights were driven.
`manifest.json` records source hashes, selection times, controls and sampling limits.
Full 512-channel frames and control messages are gzip-compressed without filtering.

`channel-groups.json` compares each fixture/property independently. Pan/tilt use
16-bit values; colours and controls use held byte values. Equivalent colour/shutter
slots are reported separately from raw errors. Active unmapped channels are errors
in coverage, never discarded. The CF805 bar is eight RGB pixels at addresses 97–120.

Use `python -m tools.compare_dmx --help` for a fixed-offset comparison. The Python
`fit_shared_channel_offset_beats` helper fits one bounded phase offset across every property.
Calibrate on 120 BPM beats 4–60, then freeze that offset for beats 68–124 at both
tempos. Never fit properties or holdout data independently.

`repeatability.json.gz` measures SoundSwitch against itself and across tempos.
`lua-before.json.gz` records the initial Lua comparison. Errors are in native DMX
units (255 for bytes, 65535 for pan/tilt); maxima retain sharp-boundary differences.
Receive cadence and output quantization prevent bit-identical cross-engine frames.
