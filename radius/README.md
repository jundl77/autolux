# Radius

Radius is Autolux's audiovisual bus, with two C++ applications:

- `radius_tx`: captures stereo audio, applies the show delay, combines local
  SoundSwitch ArtDmx with Autolux overlays, and sends the stream.
- `radius_rx`: buffers audio, follows its media clock for DMX playback, and drives
  the speakers and Enttec USB device on separate threads.

Build and test from the root with `.\build-radius.ps1`. Start the complete system
with `uv run autolux --prod`. The CLI supplies a unique session and deploys RX over
SSH. Each binary also accepts a config path, or `--check-config <path>`.

## Timing and transport

The bus uses UDP datagrams no larger than 1056 bytes. Its versioned header carries
an 8-byte session ID and an 8-byte audio-frame watermark. Audio is 44.1 kHz stereo
Float32LE. TX holds the show delay; RX starts with a 150 ms jitter target and uses
bounded clock correction. Network and audio-device latency are additional.

Audio callbacks use fixed queues and perform no network or USB I/O. Full queues
drop incoming data and count the loss. RX conceals missing audio with silence and
recovers excessive backlog instead of retaining unbounded latency.

RX schedules lighting against the audio position corrected for reported device
latency. USB refresh and device latency limit physical alignment; this is not a
sample-accurate lighting output guarantee. Loss of media blanks DMX after one
second. Both endpoints heartbeat every 10 seconds and exit after 30 seconds
without the peer heartbeat. RX sends health once per second.

`audio_core`, `network_core` and `dmx_core` optionally pin separate threads;
`-1` leaves scheduling to Windows. SoundSwitch's local Art-Net output feeds
`127.0.0.1:6454`; Autolux overlays feed port 19001. Only the Radius bus crosses
the network, on UDP 19002 by default.
