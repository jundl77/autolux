# Starfire validation

Validated on Windows on 2026-10-25. Starfire is this branch's live lighting backend.

| Check | Result |
| --- | --- |
| Python | Full run: 2,331 passed, two failures subsequently fixed and retested. Another 260 focused checks passed after the input API rename; final CLI validation passed all 53 cases, including actual dashboard HTTP/mDNS startup and bounded Ctrl-C shutdown. |
| Rust | 91 tests passed, including all Lua sequences, protocol/lifecycle handling and performance gates. Strict Clippy and rustfmt passed. |
| Radius | Both executables built; all four native test groups passed. |
| Distribution | Product build, wheel/source package and formatting passed. Generated Protobuf descriptors are packaged; isolated codec import passed. |
| Production readiness | This branch's `autolux --prod --check` passed in under three seconds: binaries, fixtures, model, capture and SSH. |
| Analysis/backtest | All ten tracks preserve 9,298 beats, complete section boundaries, sound events and every analysis score/count. Full-report hashes reflect verified lighting changes; the strict comparison gate and previously failing benchmark test pass. |
| Recorded command replay | All ten actual command streams rendered twice: 168,114 DMX frames per pass, identical repeated digests, zero script errors. Slowest run: 551.74x real time. |
| Live loopback | Python ownership to Protobuf IPC to native Art-Net: all four banks observed over 90 seconds; 3,602 packets, no sequence gaps, final blackout preserving movement. |
| Physical pipeline | A 90-second Spotify run drove Starfire, Radius TX, the remote RX and Enttec; the user confirmed working lights. Audio transport was active, but the speaker was off, so hearing remains unconfirmed. The dashboard startup defect exposed by this run is fixed and regression-tested. |
| Failure handling | Real heartbeat expiry exited at 30.000 seconds with blackout. Malformed/oversized messages, script failures, full-queue shutdown and mismatched production universes are covered. |
| Diagram | Mermaid rendered; all 12 nodes have explanations and all three application boundaries are dotted rectangles. |

## Rendering performance

Both performance tests are part of the ordinary Rust suite. They render a
90-second show with four banks, crossfades, accents, tempo changes, measured RMS,
speed and master controls.

| Patch | Required minimum | Debug measurement | Release measurement |
| --- | --- | --- | --- |
| Room: 6 fixtures | 10x real time | 132.6x | 530.4x |
| Stress: 64 fixtures, 2 universes | 2x real time | 21.9x | 82.2x |

The factor covers replay validation, engine construction, commands, Lua,
composition and fixture encoding. File parsing/output and network pacing are
excluded. These are regression budgets and measured throughput, not hard
real-time guarantees.

The live loopback used 187 recorded beat anchors and 15 authored controls.
Packet intervals were median 25.036 ms, p99 25.498 ms and maximum 26.658 ms.
Periodic render samples were 0.094-0.213 ms; those sparse samples are not
worst-case measurements.

## Scope and reproduction

The physical run reported no lost or late audio packets, buffer overflows, buffer
underruns or dropped DMX frames. One device underrun occurred during startup;
the counter stayed at one. Maximum audio packet gap was 28 ms. These are observed
session measurements, not guarantees for every Wi-Fi condition. SoundSwitch
reference calibration is in progress with local capture and physical output off.
Audible output and the calibrated looks still need a joint audition.
Radius currently stamps Art-Net ingress at receipt; exact future render targets
need a further timing handoff. The RMS input updates on detected beats.

[Analysis parity evidence](analysis-parity.json) records reference/current
hashes and the exact unchanged analysis checks. Local detailed evidence lives in
`.live/starfire-validation/`: `migration-*.log`, `backend-parity/`,
`live-pipeline-90s/validation.json`, `live-heartbeat-timeout.json` and
`performance-native-check.log`.

```powershell
.\build-starfire.ps1
uv run autolux starfire replay starfire/tests/replays/drop.json --output .live/drop.dmx
uv run autolux --prod --check-full
```

The full suite/backtest takes several minutes. Ordinary `--prod --check` is a
separate readiness check with a 45-second deadline. Validation used the existing
Python environment, explicitly resolving imports to this worktree; model/GPU
analysis ran serially and data was not copied.
