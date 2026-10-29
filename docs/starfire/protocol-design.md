# Starfire protocol

Autolux is the client; its owned Starfire process is the server.
The [protocol](../../proto/starfire/starfire.proto) imports the
[rig definition](../../proto/starfire/rig.proto); both generate the message classes.
Only top-level wire messages have direction annotations.

## Four concepts

| Command | Behavior |
| --- | --- |
| `PlayAutoloop` | Play one globally unique autoloop ID until changed. Repeating the ID continues the loop. An empty ID enters idle and clears effects. A `Transition` is a cut or a positive beat-duration crossfade; an absent one uses `DEFAULT_CROSSFADE_BEATS`. A cue's steps use the same message. |
| `SetActiveEffect` / `UnsetActiveEffect` | Set or remove an identified accent. Specify fixture properties directly; omitted properties leave the baseline untouched. An optional duration in seconds expires the effect; otherwise it lasts until unset. A special's `Envelope` starts on the beat its command applied; `instantaneous_release` drops it this frame, otherwise it runs its release. |
| `UpdateBeat` | Autolux reports one detected beat with the bar it belongs to: beat index, the tracker's bar phase (absent if unknown), beats per bar and tempo. Starfire derives the continuous bar-aligned position from those facts and interpolates at tempo between beats; animation speed never changes musical phase. `BeatFeatures` (onset, bass, mid, high, each 0..1 against the track's own peaks) ride along once measured; a beat without them clears the previous ones. |
| `SetParameter` | Change one global parameter, independently of the active autoloop. |

## Section cues

Autolux sends each section's plan as one `CueSection` when it decides it, ahead of the room.
Starfire maps the start beat onto its own grid through the newest beat, so a bar-phase
correction moves a waiting cue with the grid, and plays it on its render clock.

- **On time:** the cue starts on the first frame at or past its start beat.
- **Late** (its start has passed on receipt): it starts on the next bar line, its rotation
  counted from the true start; entry rows (begin >= 0) enter on that bar line and pre-roll
  rows (begin < 0) are skipped.
- **Supersession:** a cue drops every waiting cue starting at or after its own start, so
  Autolux re-sends cues in start order. Of several cues passing in one frame the latest
  starts; the others drop.
- **Re-cue:** a cue with the active section's start replaces it on the next bar line and
  restarts nothing: the running entry loop, its seed and every playing overlay row carry on,
  and entry rows never fire twice. It is not a next section.
- **Clearing:** `ClearCues` (sent with every Autolux queue clear) and `PlayAutoloop` end
  every cue and release their overlays; the base look plays on. Stop also drops overlays.
- **Steps:** the rotation's `Step`s play in order from the start, each for whole bars, then
  cycle.
- **Overlays:** `priority` orders overlays among themselves (higher composes later, ties keep
  list order); every `SetActiveEffect` composes above all of them. An overlay's `Envelope`
  starts on its first rendered frame and releases at its end or when its cue ends, from the
  level reached. Overlays and specials share the one `Envelope` (attack and release beats).
- **Blackout:** an overlay with `blackout` set is the blackout before the section, so it
  plays once with a negative begin. The earliest sets `beats_until_dip`; while one that
  reaches the section start holds fixtures dark, they move in black to the first step's look
  when that step cuts to a new loop. Starfire reads no meaning into bank names: Autolux's
  director sets the flag on its dip rows.
- **Lua context:** the active section's intent and elapsed beats (from the bar line at or
  before a selection without a cue), the next section's intent, beats until it, when it
  became known and beats until its dip, drop strengths, the seed, tempo and beat features;
  each is nil until known.

Bounds are errors (ErrorReply), never clamps: 8 steps, 8 overlays, 4 waiting cues, pre-roll
at most 64 beats, one-shot begins, overlay lengths, envelopes and crossfades up to 1,024
beats, a repeating row inside its period with length plus release fitting it, a blackout only
as a one-shot pre-roll. At most 8 overlays play at once; a ninth is
not started and is reported. The largest cue stays under the 4 KiB real-time cap. Overlay
faults and refusals never stop the show; they go to `Metrics.overlay_error`.

`DiscardDelayedAudio` is the one transport command: when the source stops, the embedded
Radius TX silences the audio it holds for the playback delay instead of playing it out.
Lighting is unaffected; Art-Net output ignores it.

Each `Parameter` contains a defined enum type and a number or boolean value.
It is shared by Login's initial settings and runtime updates.

| Parameter type | Value |
| --- | --- |
| `OUTPUT_BRIGHTNESS` | Number, 0..1; final brightness multiplier. |
| `ANIMATION_SPEED` | Number, 0..8; animation rate independent of musical phase. |
| `OUTPUT_BLACKOUT` | Boolean; final darkness gate. |
| `AUDIO_RMS_NORMALIZED` | Number, 0..1; measured RMS, unknown until supplied. |
| `GROUP_MASTER` | Number, 0..1, with `group` naming a rig group; inhibitive submaster. Same perceptual curve as the global master; scales intensity, and colour on fixtures without a Dimmer; pan, tilt and gobo untouched; inhibitive like grandMA3's Group Master. |

Unknown parameter types, wrong value types and invalid ranges are rejected.
There are no arbitrary parameter names or autoloop-owned parameters.

Effects target all fixtures by default, or a named group/fixture. They replace
only specified intensity, colour, pan, tilt, strobe or GDTF attribute values.
Generic attributes use physical numbers or named ChannelSet presets; qualify
ambiguous names with geometry or a full function path. Higher priority
wins; command order breaks ties.
Explicit physical attributes take precedence over convenience values on the
same channel; global brightness and blackout still apply last.
Removing an effect reveals the continuing baseline. There is no blend-mode API.

## Session and transport

Autolux owns fixture configuration and scripts. `Login` supplies a complete snapshot:

- `RigDefinition`: fixtures, addresses, movement calibration, ordered groups and
  required profiles. Each profile carries its GDTF DMX mode as Autolux compiled it (a
  typed `DmxMode`: channels, functions, sets, mode masters); fixtures reference it by ID.
  Rust validates it once at Login and parses no GDTF.
- `AutoloopDefinition` entries with globally unique autoloop IDs, bank grouping
  metadata and Lua source code, carried in `Login.autoloops`. `intensity_htp` comes from an
  overlay's `-- intensity: htp` or `ltp` header; base loops send false. Loading a directory
  (replay, check), Starfire classifies each script by its header: `energy` marks a base loop,
  `strobe` and `intensity` an overlay, and any other header is a load error naming the file.
- Initial parameters and the unicast Art-Net destination.
- The nonzero random 64-bit `run_id` generated by Autolux for this start. LoginReply
  and Metrics echo it; subsequent commands belong to the owned process stream.
- The protocol major version, which must match. There is no minor version and nothing is
  negotiated; consumers use the generated constant.

Starfire validates the patch and executes every script against it before acquiring
output. `LoginReply` means initialization succeeded and the renderer is ready for
commands. There are no asset paths, discovery step or additional configuration handshake.
Asset edits apply on a new session; parameters and effects change live.

Rust owns session resources through RAII. Failed login leaves no session;
duplicate login cannot replace a running one. Closing the process pipe ends the
session and attempts darkness while preserving movement positions.

Commands and replies are standalone messages. Each frame contains a one-byte
`MessageType`, a four-byte big-endian payload length, then the generated Protobuf
payload. Login, which carries the session assets, is bounded to 4 MiB; every
other message is real-time and bounded to 4 KiB. There are no Render envelopes or custom serializers.
Wrong directions, unknown types, oversized and truncated frames are rejected.

Commands have positive increasing IDs and apply in order at render boundaries.
Only Login has a success reply. Rejections send `ErrorReply` with the command ID
and a reason; rejection preserves accepted state. There are no automatic retries.

`Metrics` reports completed frames, missed deadlines, queue depth, render duration,
active autoloop, faults, `pending_cues`, `active_overlay_count`, `overlay_error` (at most
256 bytes; a notice, never a renderer fault) and `last_processed_command_id`. That counter
includes accepted and rejected commands; it is not a per-command success receipt.
FPS comes from frame-count and elapsed-time differences. Both endpoints send
independent ten-second heartbeats and time out after thirty seconds. Heartbeats
have no ID and are not echoed. Stalled command progress also fails supervision.
Final fault metrics are best-effort; exit status and stderr remain authoritative.

Radius owns audio watermarks and synchronized DMX playback. Renderer metrics do
not measure physical illumination.

Radius uses the same standalone message, Login, ErrorReply, Metrics and heartbeat
patterns over UDP. Its complete datagram is bounded to 1,200 bytes. `AudioFrame`
and `LightFrame` carry the shared `run_id` and 64-bit audio-frame positions; their
message types are `AUDIO_FRAME` and `LIGHT_FRAME`. TX retries the
identical Login until ready; RX replies idempotently without resetting output.
Media capture begins only after LoginReply.
Autolux initializes its owned TX through launch configuration; TX logs into RX.
Production supplies a private, exclusively bound loopback Art-Net port so foreign
traffic on standard port 6454 cannot acquire this run's identity.
