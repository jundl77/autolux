# SoundSwitch Auto Pilot

Live DJ lighting driven by musical structure. Audio passes through madmom beat
tracking, MERT music features, the l9d section model and a bar-aligned decoder.
Its nine section classes map to atmospheric, breakdown, buildup and drop looks,
sent to SoundSwitch over MIDI with OS2L rhythm and UDP DMX overlays.

The room hears audio after a 14-second delay so the model can recognize a
transition before the audience reaches it. The sibling `dmx-enttec-node` project
supplies that delay in its **audio sender**, before the network receiver.

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
without the visualizer, or `python auto_pilot list` to inspect devices.

Architecture and research history are in [CLAUDE.md](CLAUDE.md). The accepted
system includes deterministic simulation, a ten-track benchmark, corpus and
training tools, and a waveform labeler. Most trained models and corpus assets
are local under `training/data/raveform`; they are not included in a clone.
