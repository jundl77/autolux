# Sequence reference

These localhost Art-Net captures contain two 64-beat cycles of each authored
SoundSwitch loop at 120, 140 and 156 BPM. No audio or physical lights were driven.
`manifest.json` records source hashes, selection times, controls and sampling limits.
Full 512-channel frames and control messages are gzip-compressed without filtering.

`channel-groups.json` compares each fixture/property independently. Pan/tilt use
16-bit values; colours and controls use held byte values. Equivalent colour/shutter
slots are reported separately from raw errors. Active unmapped channels are errors
in coverage, never discarded. The CF805 bar is eight RGB pixels at addresses 97–120.

Use `python -m tools.compare_dmx --help` for a fixed-offset comparison. The Python
`fit_shared_channel_offset_beats` helper fits one bounded phase offset across every property.
Calibrate on 120 BPM beats 4–60, then freeze that offset for beats 68–124 at both
holdout tempos. Never fit properties or holdout data independently.

`repeatability.json.gz` measures SoundSwitch against itself and across tempos.
`lua-before.json.gz` and `lua-after.json.gz` record the Lua comparisons;
`validation.json` summarizes the final checks. Errors are in native DMX
units (255 for bytes, 65535 for pan/tilt); maxima retain sharp-boundary differences.
Receive cadence and output quantization prevent bit-identical cross-engine frames.

Run `python -m tools.check_sequence_reference --output .live/sequence-reference`
to render the actual Lua programs at 40 Hz and check every fixture property at
all three tempos. Cadence budgets were frozen from reference variation before candidate
comparison. MAE, p95 and p99 are gates; safe controls and channel coverage are exact.
Raw maxima remain diagnostics: independently sampled cuts can miss different
endpoints. `cut-diagnostics.json` identifies the remaining Drop boundary residuals:
7.6–14.7 ms, shorter than one 25 ms renderer frame.

`value-anchors.json` independently checks 80 full-universe frames through the native
renderer at their recorded times. One phase offset per bank comes from 120 BPM;
140 BPM values use a declared 5 ms clock allowance plus one quantization unit.
Controls and unassigned channels must match exactly. These checks distinguish
value errors from output cadence.
