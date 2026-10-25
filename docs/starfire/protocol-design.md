# Starfire protocol

Autolux is the client; its owned Starfire process is the server.
The [single schema](../../proto/starfire/starfire.proto) generates the message
classes. Only top-level wire messages have direction annotations.

## Four concepts

| Command | Behavior |
| --- | --- |
| `PlaySequence` | Play one globally unique sequence ID until changed. Repeating the ID continues the loop. An empty ID enters idle and clears effects. Transitions are cuts or positive beat-duration crossfades. |
| `SetActiveEffect` / `UnsetActiveEffect` | Set or remove an identified accent. Specify fixture properties directly; omitted properties leave the baseline untouched. An optional duration in seconds expires the effect; otherwise it lasts until unset. |
| `UpdateBeat` | Supply fractional beat position and tempo. Starfire interpolates between updates; animation speed does not change musical phase. |
| `SetParameter` | Change one global parameter, independently of the active sequence. |

Each `Parameter` contains a defined enum type and a number or boolean value.
It is shared by Login's initial settings and runtime updates.

| Parameter type | Value |
| --- | --- |
| `OUTPUT_BRIGHTNESS` | Number, 0..1; final brightness multiplier. |
| `ANIMATION_SPEED` | Number, 0..8; animation rate independent of musical phase. |
| `OUTPUT_BLACKOUT` | Boolean; final darkness gate. |
| `AUDIO_RMS_NORMALIZED` | Number, 0..1; measured RMS, unknown until supplied. |

Unknown parameter types, wrong value types and invalid ranges are rejected.
There are no arbitrary parameter names or sequence-owned parameters.

Effects target all fixtures by default, or a named group/fixture. They replace
only specified intensity, colour, pan, tilt or strobe values. Higher priority
wins; command order breaks ties. Global brightness and blackout apply last.
Removing an effect reveals the continuing baseline. There is no blend-mode API.

## Session and transport

Autolux owns fixture configuration and scripts. `Login` supplies a complete snapshot:

- Typed fixtures, addresses, movement calibration and ordered groups.
- Required DMX profiles, including safe channel values and colour-wheel mappings.
- Lua sources with globally unique sequence IDs and bank grouping metadata.
- Initial parameters and the unicast Art-Net destination.

Starfire validates the patch and executes every script against it before acquiring
output. `LoginReply` means initialization succeeded and the renderer is ready for
commands. There are no asset paths, discovery step or additional configuration handshake.
Asset edits apply on a new session; parameters and effects change live.

Rust owns session resources through RAII. Failed login leaves no session;
duplicate login cannot replace a running one. Closing the process pipe ends the
session and attempts darkness while preserving movement positions.

Commands and replies are standalone messages. Each frame contains a one-byte
`MessageType`, a four-byte big-endian payload length, then the generated Protobuf
payload, bounded to 64 KiB. There are no Render envelopes or custom serializers.
Wrong directions, unknown types, oversized and truncated frames are rejected.

Commands have positive increasing IDs and apply in order at render boundaries.
Only Login has a success reply. Rejections send `ErrorReply` with the command ID
and a reason; rejection preserves accepted state. There are no automatic retries.

`Metrics` reports completed frames, missed deadlines, queue depth, render duration,
active sequence, faults and `last_processed_command_id`. This progress counter
includes accepted and rejected commands; it is not a per-command success receipt.
FPS comes from frame-count and elapsed-time differences. Both endpoints send
independent ten-second heartbeats and time out after thirty seconds. Heartbeats
have no ID and are not echoed. Stalled command progress also fails supervision.
Final fault metrics are best-effort; exit status and stderr remain authoritative.

Radius owns audio watermarks and synchronized DMX playback. Renderer metrics do
not measure physical illumination.
