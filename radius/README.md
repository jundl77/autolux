# Radius

Radius is Autolux's audiovisual bus, with two C++ applications:

- `radius_tx`: captures stereo audio, applies the show delay, receives complete
  Starfire ArtDmx frames, and sends synchronized audio and lighting.
- `radius_rx`: buffers audio, follows its media clock for DMX playback, and drives
  the speakers and Enttec USB device on separate threads.

Build and test from the root with `.\build-radius.ps1`. Start the complete system
with `uv run autolux --prod`. The CLI supplies a fresh random 64-bit `run_id` and
deploys RX over SSH. Each binary also accepts a config path, or `--check-config <path>`.

## Timing and transport

The bus uses standalone Google Protocol Buffers in UDP datagrams no larger than
1200 bytes: a one-byte generated message type, four-byte big-endian payload
length, and protobuf payload. [`radius.proto`](../proto/radius/radius.proto)
defines Login, LoginReply, ErrorReply, AudioFrame, LightFrame, Heartbeat and Metrics.
Login negotiates protocol major/minor and initializes playback settings; the
reply means receiver outputs are ready. TX retries identical logins until a
reply arrives, then starts capture. Run IDs and audio watermarks use
`fixed64`; samples use packed protobuf floats at 44.1 kHz stereo.

TX holds the show delay. Login selects RX's 20-150 ms jitter target; bounded
clock correction follows the playback device. Device selection and thread
affinity stay local. Network and audio-device latency are additional.

Audio callbacks use fixed queues and perform no network or USB I/O. Full queues
drop incoming data and count the loss. RX conceals missing audio with silence and
recovers excessive backlog instead of retaining unbounded latency.

RX schedules lighting against the audio position corrected for reported device
latency. USB refresh and device latency limit physical alignment; this is not a
sample-accurate lighting output guarantee. Loss of media blanks DMX after one
second. Both endpoints heartbeat every 10 seconds and exit after 30 seconds
without the peer heartbeat. RX sends Metrics once per second. Media has no
acknowledgments, and heartbeat reception does not trigger an echo.

CMake downloads SHA256-pinned Protobuf v33.5 and Abseil 20250512.1, builds matching
`protoc`, and generates C++ under the build directory. Endpoints link the static
lite runtime without reflection; generated code is neither checked in nor formatted.
Framing, media validation and ArtDmx parsing live in [`libs/bus`](libs/bus).
The first build needs internet access. Major versions must match; the negotiated
minor is the lower supported version. Wrong run IDs, unexpected sender ports,
invalid message kinds, malformed packets and invalid media values are rejected.
The receiver pins its sender only after Login. Its local ready file means the
listener is available; the transmitter's means the run and capture are ready.
Dashboard `health.json` uses seconds, integer counters and null ages for missing input.

`audio_core`, `network_core` and `dmx_core` optionally pin separate threads;
`-1` leaves scheduling to Windows. Production assigns Starfire and TX a private
loopback UDP port, preventing other Art-Net producers from entering the show.
Standalone TX defaults to `artnet_port: 6454`; custom ports bind exclusively.
Only the Radius bus crosses the network, on UDP 19002 by default.

Every Radius packet carries `run_id`; mismatched runs are rejected before media
enters playback buffers. A restart generates a new ID for both audio and lighting.
