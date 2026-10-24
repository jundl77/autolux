# Autolux

Live DJ lighting driven by musical structure. Audio passes through madmom beat
tracking, MERT music features, the l9d section model and a bar-aligned decoder.
Its nine section classes map to atmospheric, breakdown, buildup and drop looks,
sent to SoundSwitch over MIDI with OS2L rhythm and UDP DMX overlays.

The room hears audio after a 14-second delay so the model can recognize a
transition before the audience reaches it. The native **audio sender** supplies
that delay before the network receiver.

Autolux brings SoundSwitch Auto Pilot and dmx-enttec-node into one repository,
preserving both Git histories. Their processes and responsibilities stay separate:

| Location | Responsibility |
| --- | --- |
| `lib/` | Python analysis, section decisions, scheduled MIDI/OS2L and overlays |
| `simulate/`, `training/`, `tests/` | Replay, visualizer, model work and Python verification |
| `native/app_audio_sender/` | C++ capture, playback delay and UDP transmission |
| `native/app_audio_receiver/` | Network audio reception and speaker playback |
| `native/app_dmx_usb_node/` | Art-Net and overlay reception, Enttec DMX output |
| `native/libs/` | Native transport, audio, protocol and device libraries |

Native packets remain compatible with the existing receiver. A shared 64-bit
media timestamp protocol is deferred to a separate design change.

Install Python 3.11, uv and Visual Studio C++ Build Tools, then prepare a checkout:

```powershell
git submodule update --init --recursive
uv sync --extra dev --extra visualizer
.\build-native.ps1
```

On the configured Windows setup, start SoundSwitch in Performance Mode with
VirtualDJ/OS2L enabled, leave MIDI mapping mode, and run:

```powershell
.\start-show.ps1 -CheckOnly
.\start-show.ps1
# If the standalone sender is already running:
.\start-show.ps1 -ReplaceAudioSender
```

The launcher starts loopMIDI if necessary, resolves the MIDI and audio devices
by name, checks the model and matching capture source, and starts the sender and
autopilot together. It derives the sender delay from the autopilot setting on
every launch. `-ReplaceAudioSender` restarts only a sender from this project's
configured binary path; it refuses to stop one from another location.

Route the music to **CABLE Input**. Both programs capture the undelayed **CABLE
Output**; only the sender delays audio. The receiver on `192.168.178.245` plays
it, while SoundSwitch sends Art-Net to the DMX node on that machine. Network and
audio-device latency remain in addition to the configured delay. Do not also
monitor the cable directly to the room or add a second 14-second receiver delay.

The visualizer is at http://127.0.0.1:8050. Ctrl-C stops both programs. Sender
logs and its generated configuration live under `.live/`. Use `-NoUi` to run
without the visualizer, or `uv run autolux list` to inspect devices. The
`auto_pilot` command remains available for existing scripts.

Architecture and research history are in [CLAUDE.md](CLAUDE.md). The accepted
system includes deterministic simulation, a ten-track benchmark, corpus and
training tools, and a waveform labeler. Most trained models and corpus assets
are local under `training/data/raveform`; they are not included in a clone.
Linked worktrees find the main checkout's corpus automatically. On another
machine, set `RAVEFORM_DATA_DIR` to the corpus directory. The original local
checkouts and their research artifacts remain intact.
