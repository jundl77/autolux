# Starfire validation

Validation covers the native renderer, controller integration, transport and
fixture definitions. Hardware observations are separate from deterministic checks.

## Automated coverage

- Every authored autoloop has a full-DMX replay digest and behavior assertions.
  Tests cover timing corrections, selection continuity, transitions, accents,
  blackout, bounded Lua and repeatable rendering.
- Generated Python-to-Rust messages exercise Login initialization, major/minor
  negotiation, typed parameters, command ordering, errors, heartbeats and cleanup.
- Imported GDTF profiles are tested through real Login and loopback output,
  including exact 8/16-bit encoding. All five shipped definitions pass the
  official GDTF XML schema. Unsupported mappings fail before acquiring output.
- Radius passes four native test groups and 20 process/lifecycle checks. Media
  waits for LoginReply; retries preserve initialized resources. Endpoint tests
  disable hardware output.
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

A 90-second Spotify run through Starfire, Radius, Focusrite and Enttec received
user confirmation that audio and lights worked. A white beat pulse was judged
on-beat. After preparing models before capture, the repeat was also judged
synchronized. The dashboard measured 40 renderer frames per second.

That session had no lost audio frames, queue overflows, audio queue underruns or
dropped DMX frames. One audio-device underrun occurred at startup and stayed at
one. The maximum observed packet gap was 88 ms. These are session measurements,
not guarantees for every network condition. See
[physical evidence](calibration/physical-validation.json) and
[native loopback evidence](calibration/live-loopback.json).

Software scheduling and USB write counters do not measure physical flash
latency or DMX refresh. Radius stamps Art-Net ingress at receipt; explicit future
render targets remain a timing improvement. Measured RMS updates on detected beats.

## Limits and reproduction

GDTF support is a validated subset. The 64 KiB live Login budget includes all
fixture XML and Lua sources; importing a large definition does not enlarge it.
No physical audition or full GPU analysis rerun accompanied the GDTF changes.

```powershell
.\build-starfire.ps1
uv run autolux starfire replay starfire/tests/replays/drop.json --output .live/drop.dmx
uv run autolux --prod --check
uv run autolux --prod --check-full
```
