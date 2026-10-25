# Starfire validation

## Current system

Autolux starts Starfire with an embedded Rust Radius TX worker and deploys Rust RX
through SSH. One TCP connection carries audio, lighting, metrics and heartbeats.
The 14-second analysis delay and 500 ms receiver buffer are separate. Audio
references its lighting state; RX schedules that state against audible audio.
Starfire and Radius have independent protobuf contracts and telemetry.

## Physical validation

- A 300-second production run completed with clean shutdown at both ends. The user
  confirmed music and responsive lighting. All four lighting categories appeared.
- A fresh 30-second run also completed cleanly. The user confirmed that the first
  audible music and lighting started together.
- Both runs recorded zero audio/device underruns, rebuffer events, capture
  overruns, lost/concealed/overflow audio, and dropped DMX frames.
- During the five-minute run the playing buffer ranged from 471 to 516 ms. The
  largest sampled audio delivery gap was 113 ms; buffering absorbed it without
  interrupting playback. The measurements do not identify the gap's cause.
- Starfire rendered 11,963 frames and missed 39 render deadlines over 300 seconds
  (about 39.9 FPS). The startup repeat missed none. No Lua errors occurred.
- The dashboard and Prometheus endpoint served current renderer and transport
  measurements. Both local Starfire and remote RX were absent after shutdown.

See [production evidence](calibration/rust-production-validation.json) and the
[earlier Rust hardware proof](calibration/rust-device-proof.json). Physical timing
was judged by the user, not measured with an audio loopback or photodiode.
These physical runs precede the review hardening below; the review used offline
replay, loopback transport and browser checks.

## Automated coverage

- Radius: 38 Rust tests covering framed TCP, ordering, run identity, backpressure,
  audio/light association, 500 ms buffering, clock drift, starvation tails,
  exact ENTTEC packets, bounded shutdown and bidirectional ten-second heartbeats.
- Starfire: 137 Rust tests covering deterministic Lua-to-DMX replay, fixture
  encoding, sandbox bounds, safe darkness, protocol validation and embedded TX.
- Strict Clippy and Rust formatting pass. Workspace builds run these checks and
  generate language bindings from the root schemas.
- The broad Python suite passed 2,504 tests in 484.74 seconds and exposed one
  expected checksum change from retaining the audible tail. A fresh ten-track
  backtest and [exact report audit](silence-tail-parity.json) verified seven
  unchanged reports and three containing only the intended tail correction.
  Every score, beat, section decision and tolerance is unchanged. The full-set
  gate passes against the audited expectations; the full suite was not repeated.
- Manual training corrections pass five new regressions and 138 existing dataset,
  downbeat and exclusion checks. No models were retrained.
- Browser checks on a copied rig cover map selection, automatic addressing,
  conflicts, save/remove and group preservation. No JavaScript exceptions occurred.
- Workspace/release builds, Python packaging and formatting pass.
- Production readiness completed in 4.71 seconds. `--prod --check` has a 45-second
  deadline; full builds, suites, backtests and simulations use `--check-full`.

See the [code review](code-review.json) for findings, fixes and review scope.

## Rendering performance

The Rust suite renders a 90-second replay with all four banks, transitions,
accents, tempo, measured RMS, speed and brightness. It requires at least 10x real
time for the 14-fixture room and 2x for 64 fixtures. These are throughput checks,
not guarantees of physical output latency. Every authored autoloop retains a
checked full-DMX digest and behavior assertions.

## Scope still open

GDTF interpretation remains a validated subset. Complete GDTF/MVR asset
preservation and replacing custom rig/profile JSON remain pending.
The 64 KiB Login limit includes fixture XML and Lua sources. XML carriage
alone is not full renderer conformance.

```powershell
.\build.ps1
uv run autolux --prod --check
uv run autolux --prod --check-full
```
