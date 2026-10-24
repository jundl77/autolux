# Autolux native transport

The former dmx-enttec-node project, now maintained under `native/` in Autolux.

A super simple artnet to Enttec DMX USB Pro application. Listen to artnet, and replay it into the Enttec DMX USB Pro.

Built for windows, but it shouldn't be too difficult to make it work on linux.

### Install

1. From the Autolux root: `git submodule update --init --recursive`.
2. Run `./build-native.ps1`, or build this directory with CMake.


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

Use the repository root's `start-show.ps1` to generate a sender
configuration from its current delay and start both processes. Do not run a
second sender alongside it or delay the receiver a second time.

Build `app_audio_sender` and `audio_delay_test`, then run `ctest --test-dir
<build-directory> --output-on-failure`. Tests cover exact frame delay at 44.1 and
48 kHz, stereo ordering, callback sizes, wraparound and reset.
