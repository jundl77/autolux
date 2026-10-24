# Operating Autolux

Start with `uv run autolux --prod`. Use `--check` to validate the model, devices,
configuration and SSH access without starting a show. Stop with Ctrl-C or
`uv run autolux stop`.

## Health

The dashboard combines analysis and lighting with six system indicators:
controller state, bidirectional link, recent audio glitches, buffered audio,
lighting connectivity and lighting scheduling lateness. Glitch indicators cover
the last ten seconds; lifetime counters remain available to Prometheus.

- **No link:** check receiver power, SSH access and UDP 19002. Heartbeats run every
  ten seconds; missing heartbeats end the session after thirty seconds.
- **Audio glitches:** capture overruns identify input-device pressure; RX underruns
  identify missing playback data. Lost frames and queue drops distinguish transport
  trouble from device failures. One startup device underrun is visible separately.
- **Buffer drift:** the default target is 150 ms to absorb Wi-Fi delivery bursts. Clock correction handles slow
  device-clock differences; repeated latency recovery indicates sustained stalls.
  Audio delivery-gap and signal-peak metrics distinguish delayed packets from silence.
- **Lighting unavailable:** confirm SoundSwitch Performance Mode, Art-Net output to
  localhost, the configured universe, and the receiver's Enttec connection.
- **Analysis degraded:** inspect the GPU fault and timing panels. Rhythm continues
  while the analysis worker recovers.

The 14-second show delay is applied once. RX jitter and audio-device buffering add
latency to audio and its watermarked lighting together. DMX scheduling lateness
measures software scheduling, not the physical response time of fixtures.

## Metrics and logs

Prometheus scrapes `http://127.0.0.1:8050/metrics` (8051 with `--no-ui`). Use
`docs/prometheus.yml` as a local scrape configuration, and point Grafana at that
Prometheus server if longer-term history is needed. Metrics use seconds, cumulative
counters and fixed endpoint labels. Stale endpoint data is unavailable, never zero.

`.live/current.json` identifies the latest session. Each session retains generated
configs, TX output and receiver SSH output. The receiver keeps its own session
logs under `%LOCALAPPDATA%\Autolux\runs`. No credentials are stored in the repo.
