# Starfire validation

Validated on Windows on 2026-10-25. Starfire is this branch's live lighting backend.

| Check | Result |
| --- | --- |
| Python | Prior full suite: 2,398 passed in 7m50s. Controller-owned Login: 76 focused CLI/production checks passed, including generated Python-to-Rust messages, asset preservation/bounds, initialization/runtime brightness, error replies, stalled progress and shutdown darkness. |
| Startup/dashboard follow-up | 226 tests passed: CLI lifecycle, delayed capture/song clocks, telemetry freshness/rates, controller/client/replay, simulation and committed analysis digest comparisons. Desktop browser review covered Control, Telemetry and Config without browser errors or horizontal overflow. |
| Rust | Controller-owned Login: 41 focused protocol, lifecycle, fixture and replay checks passed. Supplied assets reproduce file-loaded DMX; probing leaves no playback state. Authored-loop hashes are unchanged. The preceding full suite and performance gates passed; strict Clippy, rustfmt and the release build pass. |
| Radius | Both executables built; all four native test groups passed. |
| Distribution | Wheel/source package and formatting passed. Standard generated Python Protobuf bindings and typing stubs are packaged; isolated wheel import and message round-trip passed without repository access. Eight build failure/propagation tests passed. |
| Production readiness | This branch's `autolux --prod --check` passed in 2.2 seconds: binaries, Lua against the saved fixtures, required sequence identities, model, capture and SSH. |
| Analysis/backtest | All ten tracks preserve 9,298 beats, complete section boundaries, sound events and every analysis score/count. Full-report hashes reflect verified lighting changes; the strict comparison gate and previously failing benchmark test pass. |
| Recorded command replay | All ten actual command streams rendered twice on the calibrated 14-fixture patch: 168,114 DMX frames per pass, identical repeated digests, zero script errors. Final release also matches the prior build exactly; slowest run: 473.1x real time. |
| Live loopback | Python ownership to Protobuf IPC to native Art-Net: all four banks observed over 90 seconds; 3,602 packets, no sequence gaps, final blackout preserving movement. |
| SoundSwitch calibration | All four loops pass independent DMX comparisons at 120, 140 and 156 BPM; complete channel coverage and exact safe controls. See [reference calibration](calibration/README.md) for timing and quantization limits. |
| Physical pipeline | A 90-second Spotify run exercised all four categories through Starfire, Radius, Focusrite and Enttec; the user confirmed audio and lights. A simple white beat pulse was judged on-beat. After moving model preparation ahead of capture, the user confirmed the repeat remained synchronized. See [physical validation](calibration/physical-validation.json). |
| Failure handling | Real heartbeat expiry exited at 30.000 seconds with blackout. Malformed/oversized messages, script failures, full-queue shutdown and mismatched production universes are covered. |
| Diagram | Mermaid rendered; all 12 nodes have explanations and all three application boundaries are dotted rectangles. |

## Rendering performance

Both performance tests are part of the ordinary Rust suite. They render a
90-second show with four banks, crossfades, accents, tempo changes, measured RMS,
speed and master controls.

| Patch | Required minimum | Debug measurement | Release measurement |
| --- | --- | --- | --- |
| Room: 14 fixtures | 10x real time | 85.7x | 415.5x |
| Stress: 64 fixtures, 2 universes | 2x real time | 27.3x | 121.1x |

The factor covers replay validation, engine construction, commands, Lua,
composition and fixture encoding. File parsing/output and network pacing are
excluded. These are regression budgets and measured throughput, not hard
real-time guarantees.

The live loopback used 187 recorded beat anchors and 15 authored controls.
Packet intervals were median 25.018 ms, p99 25.583 ms and maximum 26.859 ms.
Periodic health samples reported no skipped frames, queued controls or script
errors; sparse samples are not worst-case latency measurements.

## Scope and reproduction

The full physical run reported no lost or late audio packets, buffer overflows,
queue underruns or dropped DMX packets. One device underrun occurred at startup;
the counter stayed at one. Maximum observed packet gap was 88 ms. A separate
restart encountered an audio stream error and recovered on a clean retry.
These are session measurements, not guarantees for every Wi-Fi condition.
The corrected-startup audition measured 40 renderer frames/s in the dashboard.
USB writes and pre-USB scheduling lateness do not measure physical DMX refresh
or fixture response latency. SoundSwitch calibration used isolated local capture.
Radius currently stamps Art-Net ingress at receipt; exact future render targets
need a further timing handoff. The RMS input updates on detected beats.

[Analysis parity evidence](analysis-parity.json) records reference/current
hashes and the exact unchanged analysis checks. Local detailed evidence lives in
`.live/starfire-validation/`: `migration-*.log`, `backend-parity/`,
`live-pipeline-90s/validation.json`, `live-heartbeat-timeout.json` and
`performance-native-check.log`, `final-room-validation/summary.json` and
`calibrated-live-90s/validation.json`.

```powershell
.\build-starfire.ps1
uv run autolux starfire replay starfire/tests/replays/drop.json --output .live/drop.dmx
uv run autolux --prod --check-full
```

The full suite/backtest takes several minutes. Ordinary `--prod --check` is a
separate readiness check with a 45-second deadline. Validation used the existing
Python environment, explicitly resolving imports to this worktree; model/GPU
analysis ran serially and data was not copied.
