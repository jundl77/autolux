# Starfire validation

Validation covers the native renderer, controller integration, transport and
fixture definitions. Hardware observations are separate from deterministic checks.

## Current integration status

Autolux supplies one nonzero 64-bit `run_id` to Starfire and both Radius endpoints.
Production uses a private loopback Art-Net ingress. The dashboard retains its top
title with Control, Telemetry and Config directly beside it; decorative labels
are removed.

The startup baseline now begins at the 14-second room-audio delay without waiting
for a committed section. This correction passes automated tests but has not yet
been repeated on hardware. The requested 300-second audition did not complete.

## Automated coverage

- Every authored autoloop has a full-DMX replay digest and behavior assertions.
  Tests cover timing corrections, selection continuity, transitions, accents,
  blackout, bounded Lua and repeatable rendering.
- Generated Python-to-Rust messages exercise Login initialization, major/minor
  negotiation, typed parameters, command ordering, errors, heartbeats and cleanup.
- Imported GDTF profiles are tested through real Login and loopback output,
  including exact 8/16-bit encoding. All five shipped definitions pass the
  official GDTF XML schema. Unsupported mappings fail before acquiring output.
- The latest Radius build passes all four native test groups after capture
  diagnostic changes. Earlier transport validation passed 20 process/lifecycle
  checks. Media waits for LoginReply; retries preserve initialized resources.
  Endpoint tests
  disable hardware output.
- Latest focused Python checks pass: 151 startup/controller cases and four
  capture-diagnostic cases. The shared-run protocol slice passed 79 Python
  checks; these are separate, overlapping runs, not one combined suite. The
  earlier full Starfire suite passed 124 tests.
- Source/wheel builds, isolated generated-binding imports, Rustfmt, Clippy and
  production readiness pass. `uv run autolux --prod --check` has a 45-second
  deadline; full suites and GPU backtests remain separate.
- Ten cached backtest reports retain every analysis value and score after the
  autoloop rename. Only 120 effect labels and resulting full-report checksums
  change. See [analysis parity](analysis-parity.json) and
  [report parity](autoloop-report-parity.json).

## Rendering performance

The ordinary Rust suite renders a 90-second show with all four banks,
transitions, accents, tempo changes, measured RMS, speed and brightness controls.

| Patch | Required minimum | Release measurement |
| --- | --- | --- |
| Room: 14 fixtures | 10x real time | 349x, 0.258 seconds |
| Stress: 64 fixtures | 2x real time | 81x, 1.110 seconds |

The factor includes command application, Lua, composition and fixture encoding;
it excludes file output and network pacing. It measures throughput rather than
worst-case frame latency.

## Physical observations

An earlier 90-second Spotify run through Starfire, Radius, Focusrite and Enttec
received user confirmation that audio and lights worked. A white beat pulse was judged
on-beat. After preparing models before capture, the repeat was also judged
synchronized. The dashboard measured 40 renderer frames per second.

That session had no lost audio frames, queue overflows, audio queue underruns or
dropped DMX frames. One audio-device underrun occurred at startup and stayed at
one. The maximum observed packet gap was 88 ms. These are session measurements,
not guarantees for every network condition. See
[physical evidence](calibration/physical-validation.json) and
[native loopback evidence](calibration/live-loopback.json).

The 2026-10-25 17:02 UTC attempt requested 300 seconds and ended about 51 seconds
after media startup. Both audio captures failed when a Windows RDP-session
disconnect was observed at 18:03:54 local time; this correlation does not by itself
prove the cause. The user heard audio and saw lighting, described synchronization
as partial, and reported lighting starting about five seconds late.

The final transport snapshot showed zero queue drops, lost/overflow/late audio
frames, or dropped DMX frames. One startup audio-device underrun remained; the
maximum audio packet gap was 28 ms. These counters do not turn the interrupted
run into a successful 300-second test. See the
[interrupted-run evidence](calibration/interrupted-system-test.json).

Software scheduling and USB write counters do not measure physical flash
latency or DMX refresh. Radius stamps Art-Net ingress at receipt; explicit future
render targets remain a timing improvement. Measured RMS updates on detected beats.

## Limits and reproduction

GDTF support is a validated subset. The 64 KiB live Login budget includes all
fixture XML and Lua sources; importing a large definition does not enlarge it.
Full GDTF/MVR asset preservation and broader fixture interpretation remain pending.
The proposed paired audio/light transport is not implemented; Radius currently
transports separate audio and light frames. A complete 300-second hardware run,
including startup timing, is still required. No full GPU analysis rerun accompanied
these integration changes.

```powershell
.\build-starfire.ps1
uv run autolux starfire replay starfire/tests/replays/drop.json --output .live/drop.dmx
uv run autolux --prod --check
uv run autolux --prod --check-full
```
