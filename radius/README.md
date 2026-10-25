# Radius

Radius transports synchronized stereo audio and lighting. Starfire owns the Rust
TX worker; `radius_rx` drives the remote audio endpoint and ENTTEC USB Pro.
Run the complete system through `uv run autolux --prod`.

One TCP connection carries the standalone messages in
[`radius.proto`](../proto/radius/radius.proto): a one-byte message type, four-byte
big-endian payload length, and generated Protobuf payload. Login validates the
shared random `run_id`, negotiates protocol major/minor, and opens outputs before
LoginReply. Every message is bounded to 1200 bytes. Both endpoints heartbeat
every 10 seconds and fail after 30 seconds without the peer heartbeat.

Audio is 44.1 kHz stereo Float32. TX applies the analysis delay once. Each audio
block references the preceding lighting state; RX admits them together. A light
message alone never changes the fixtures. DMX follows the audible sample position,
including the audio device's reported latency, with at most 40 updates per second.

RX fills 500 ms before playback. Starvation produces silence and darkness, then
requires another 500 ms before resuming. Valid buffered audio continues during
network interruptions. Queues are bounded; capture or playback faults stop the
run instead of silently losing synchronization. Audio callbacks allocate nothing
and perform no network, logging or USB work.

`radius_rx CONFIG.json` uses `AUTOLUX_RUNTIME_DIR` for `ready`, `stop` and
`health.json`. The ready file means the TCP listener exists; LoginReply means
hardware is ready. `--check-config` verifies the configured audio endpoint without
starting playback; `--list-devices` reports exact endpoint IDs. Device fallback is
never implicit. `network_core` and `dmx_core` optionally pin their workers.
TX publishes `transport-health.json` once per second; Radius telemetry stays
independent of the Starfire protocol. Report-write failures do not stop playback.

Build with `cargo build --release -p radius`; validate with `cargo test -p radius`
and `cargo clippy -p radius --all-targets -- -D warnings`. Tests cover framing,
backpressure, run ownership, heartbeats, audio/light admission, buffering, clock
drift and bounded USB I/O without operating physical outputs.
