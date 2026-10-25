# Starfire validation

Current branch uses Starfire for live lighting. Runtime reference: `04ccbfa`.
Validated on Windows on 2026-10-25. The full ten-track comparison is in progress;
its report hashes must be reconciled before the validation set is complete.
Physical fixture audition and exact future-watermark handoff remain separate.

## Current live integration

| Check | Result |
| --- | --- |
| Full Python run | 2,331 passed; two failures identified. The build-test dependency is fixed; full report checksum comparison is still in progress. |
| Focused final checks | 260 passed after the measured-RMS API rename, including the build-test fix and Python-to-Rust process integration. |
| Rust | 88 tests passed; strict Clippy, rustfmt and release build passed. All four sequence reference digests remain unchanged at default RMS level. |
| Radius | Both executables built; four native test groups passed. |
| Distribution | Full product build, wheel and source package passed. Protobuf descriptors ship inside the wheel. |
| Production readiness | The new branch's `autolux --prod --check` passed in under three seconds, including Starfire, fixtures, model, capture and SSH checks. |
| Live loopback | 90 seconds through Python process ownership, Protobuf IPC and native Art-Net: all four banks observed; 3,602 packets, zero sequence gaps, final blackout with movement preserved. |
| Failure handling | Missing-controller timeout exited at 30.000 seconds with blackout; malformed/oversized messages, script failures and full-queue shutdown are covered. |
| Diagram | Mermaid rendered successfully; all 12 nodes have explanations and all three application boundaries are dotted rectangles. |

The live loopback used 187 recorded beat anchors and 15 authored controls.
Packet intervals: median 25.036 ms, p99 25.498 ms, maximum 26.658 ms.
It exercised the real local control/output path without speakers or physical fixtures.

Current evidence: `.live/starfire-validation/migration-*.log`,
`live-pipeline-90s/validation.json` and `live-heartbeat-timeout.json`.

## Deterministic renderer reference

The preceding renderer baseline established these additional checks:

| Check | Result |
| --- | --- |
| Full Python suite | 2,343 passed; one missing-warm-cache case subsequently passed after simulation populated the cache. All 2,344 collected cases exercised. |
| Rust | 77 tests passed, including every registered Lua sequence; rustfmt and Clippy with warnings denied passed; release executable built. |
| Radius | Both executables built; all four native test groups passed. |
| Packaging and formatting | Python wheel/source package built; Python, C++/Protobuf and Rust formatting passed. |
| Committed backtest | All ten tracks match the recorded baseline, including complete report checksums. No baseline changes. |
| Standalone simulation | Passed; four lighting intents and 16 intent transitions observed. |
| Fixture setup | Manual Edge exercise passed: save, inversion, invalid-input rejection, reload, group-order preservation and tab switching. No UI rendering tests added. |
| Root CLI lifecycle | Loopback cancellation produced blackout, terminated the owned renderer and removed its temporary stop signal. |

## Deterministic and paced output

The 90-second audition used 187 recorded beat anchors and ten explicitly authored
control events: four banks, fades, a short strobe, master intensity and one held
fixture colour followed by clearing. This evaluates rendering/control behavior;
the authored schedule is not evidence of music-classifier accuracy.

- Two offline captures were byte-identical. The final release executable also
  reproduced that capture after the final runtime fixes.
- All 3,602 loopback datagrams matched the expected payloads in order: 3,601
  rendered frames plus the final blackout. No frame drops or sequence gaps.
- Paced duration: 90.00038 seconds. Median interval: 25.034 ms; p99: 25.416 ms;
  maximum: 25.925 ms. Maximum relative schedule error: 0.930 ms.
- Final blackout preserved mover positions; the child exited successfully.
- DMX digest: `0fdc23a028c35f0910a93295cc1627e79f0ed6a64981760b603a0a31253adfa6`.

These are controller-local loopback measurements under concurrent compilation.
They do not establish Wi-Fi, USB, physical fixture timing, colour or calibration.
No overnight test sent output to real fixtures or speakers.

## Evidence and reproduction

Local artifacts in this worktree:

- `.live/validation/20261025-015658/`: complete build, Python, backtest and
  simulation logs/results. Its revision field records the run-start revision;
  runtime slices were committed during the run.
- `.live/starfire-validation/final-rust-build.log`: final Rust checks/release build.
- `.live/starfire-validation/warm-cache-retest.log`: the initially skipped case.
- `.live/starfire-validation/paced-90s/`: replay input, authored control schedule,
  captures, packet timing, received datagrams and validation results.
- `.live/starfire-validation/fixture-tab.png`: manually reviewed fixture setup.
- `.live/starfire-validation/pointer-identity/`: reproduced allocator-identity
  nondeterminism before removal of pointer formatting from Lua.

```powershell
.\build-starfire.ps1
uv run autolux starfire replay starfire/tests/replays/drop.json --output .live/drop.dmx
uv run autolux --prod --check-full
```

The fresh worktree's native build took 17m42s, Python suite 9m47s, backtest
15m01s and standalone simulation 55s. Native dependencies and analysis caches
were cold. The ordinary `--prod --check` remains a separate readiness check with
a 45-second timeout; it does not run these full suites.

Validation reused the existing Python environment with synchronization disabled,
while imports resolved to this worktree. GPU workloads ran serially. Models and
corpus data were not copied into the worktree.
