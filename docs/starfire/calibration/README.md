# Lighting reference calibration

All four Lua autoloops pass comparison against captured SoundSwitch output at
120, 140 and 156 BPM. Each capture contains two complete 64-beat cycles, using
the same fixture patch, full master and no external accents. All output was
captured locally; speakers and physical lights stayed off.

The programs reproduce the authored palettes, individual fixture chases,
movement paths and brightness envelopes. The patch includes two movers, four
washes and eight CF805 RGB cells. Mini Kinta's channels remain dark.

| Check | Result |
| --- | --- |
| Recorded output | 12 comparisons; 26,912 Starfire frames; all cadence gates pass |
| Direct values | 80 independent captured frames pass tighter native renderer checks |
| Complete universe | 30 control channels exact; 432 unassigned channels dark; no active channels omitted |
| Determinism | Ten real Autolux command recordings rendered twice: 336,228 frames, identical repeat hashes, zero script errors |
| Live process | 90 seconds through Python, Protobuf and native Art-Net; 3,602 packets, no sequence gaps, clean blackout/shutdown |

These are calibrated matches, not byte-identical asynchronous recordings.
SoundSwitch emitted approximately 37.8 frames/second; Starfire emits 40.
Colour rounding can differ by one byte. Remaining abrupt-cut outliers lasted
7.6-14.7 ms, below one 25 ms Starfire frame; raw maxima remain in the evidence.

Each fixture property is checked separately. Cadence limits come from the
reference's measured variation and were fixed before evaluating Lua. One phase
offset per autoloop is fitted at 120 BPM and held unchanged at 140/156 BPM.
Direct-value checks allow a declared 5 ms clock uncertainty plus one quantization
unit; control channels must match exactly. No per-channel time shifts are used.

The tests exposed and fixed uneven Drop keyframe timing, brightness pulses
freezing with animation speed, and startup accepting incompatible saved patches.
Both deterministic replay and captured-value tests run in the ordinary suites.

```powershell
cargo test --locked --manifest-path starfire/Cargo.toml
uv run pytest tools/tests/test_autoloop_reference.py -q
uv run python -m tools.check_autoloop_reference --output .live/autoloop-reference
```

[Capture provenance, controls and detailed comparisons](../../../starfire/tests/reference/soundswitch/README.md)
are committed with the tests. [Recorded-stream evidence](product-validation.json)
and [live-loopback evidence](live-loopback.json) retain the final product checks.
[Product validation](../validation.md) records broader coverage. An audible
audition and physical review of the calibrated looks remain for the user's return.
