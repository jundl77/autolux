# dmx-enttec-node

A super simple artnet to Enttec DMX USB Pro application. Listen to artnet, and replay it into the Enttec DMX USB Pro.

Built for windows, but it shouldn't be too difficult to make it work on linux.

### Install

1. Clone submodules: `git sm update --init`
2. Build with cmake.


### Dependencies

- njson
- libsoundio

### Delayed room audio for SoundSwitch Auto Pilot

`app_audio_sender/audio_sender.json` sets `playback_delay_seconds` to the
autopilot's playback budget. The sender captures the undelayed virtual cable,
holds complete stereo frames in a fixed-size delay line, then sends them using
the existing UDP audio protocol. The room receiver requires no protocol or
binary change. Its ordinary network/device latency remains additional.

The buffer emits silence while filling, preserves channel order across callback
boundaries, and is reset when the capture device is reopened. A missing delay
setting keeps legacy immediate forwarding; delayed mode uses Float32LE.

Use the sibling autopilot project's `start-show.ps1` to generate a sender
configuration from its current delay and start both processes. Do not run a
second sender alongside it or delay the receiver a second time.

Build `app_audio_sender` and `audio_delay_test`, then run `ctest --test-dir
<build-directory> --output-on-failure`. Tests cover exact frame delay at 44.1 and
48 kHz, stereo ordering, callback sizes, wraparound and reset.
