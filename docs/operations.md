# Operating Autolux

Start with `uv run autolux --prod`. Use `--check` for model, device, configuration
and SSH readiness within 60 seconds, or `--check-full` for builds, all tests,
backtest and simulation. Both leave outputs stopped. Stop a show with Ctrl-C or
`uv run autolux stop`.

## Health

The dashboard at `http://autolux.local:8765` groups playback, tempo, intent and effect
above the show timeline. Transport charts retain five minutes of receiver buffer,
audio level, interruptions, lost/dropped audio frames, scheduling error and DMX
delivery. Counters appear as rates; missing reports and counter resets leave gaps.
Analysis and per-command details are under **Analysis & command diagnostics**.
Link, telemetry and hardware faults appear only when they need attention.

- **No link:** check receiver power, SSH access and TCP 19002. Heartbeats run every
  ten seconds; missing heartbeats end the session after thirty seconds.
- **Audio glitches:** capture overruns identify input-device pressure; RX underruns
  identify missing playback data. Lost frames and queue drops distinguish transport
  trouble from device failures. Device errors stop the session with an actionable log.
- **Buffer drift:** the target is 500 ms to absorb Wi-Fi delivery bursts. Clock correction handles slow
  device-clock differences; repeated rebuffering indicates sustained stalls.
  Audio delivery-gap and signal-peak metrics distinguish delayed packets from silence.
- **Lighting unavailable:** inspect the Starfire process log, fixture patch and Lua
  autoloop, the selected DMX universe, and the receiver's Enttec connection.
- **Analysis degraded:** inspect the GPU fault and timing panels. Rhythm continues
  while the analysis worker recovers.

The 14-second show delay is applied once. RX jitter and audio-device buffering add
latency to audio and its watermarked lighting together. DMX scheduling lateness
measures software scheduling, not the physical response time of fixtures.

## Metrics and logs

Console logs show startup, lighting changes, a music summary every ten seconds,
and system health every thirty seconds or when its state changes. HTTP access logs
are suppressed; server errors and recovery messages remain visible.
Use `uv run autolux --prod --verbose` for beat, note and protocol diagnostics.

Prometheus scrapes `http://autolux.local:8765/metrics`; headless mode serves local
metrics at `http://127.0.0.1:8766/metrics`. Use
`docs/prometheus.yml` as a local scrape configuration, and point Grafana at that
Prometheus server if longer-term history is needed. Metrics use seconds, cumulative
counters and fixed endpoint labels. Stale endpoint data is unavailable, never zero.

`.live/current.json` identifies the latest session. Each session retains generated
configs, Starfire output (including embedded TX) and receiver SSH output. The receiver keeps its own session
logs under `%LOCALAPPDATA%\Autolux\runs`. No credentials are stored in the repo.
