# Starfire protocol

Autolux is the client; its owned Starfire process is the server.
The [single schema](../../proto/starfire/starfire.proto) generates the message
classes. Only top-level wire messages have direction annotations.

## Four concepts

| Command | Behavior |
| --- | --- |
| `PlaySequence` | Play one bank/sequence until changed. Repeating its identity continues the loop. Both names empty enters idle and clears effects. Transitions are cuts or positive beat-duration crossfades. |
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

`Login` supplies the rig, sequence directory, Art-Net destination and initial
parameters. Rust constructs one owning session and sends `LoginReply` after
successful initialization. RAII releases partial initialization and attempts
darkness on shutdown. Failed login leaves no session; duplicate login cannot
replace a running one. Closing the process pipe closes the session.

Commands and replies are standalone messages. Each frame contains a one-byte
`MessageType`, a four-byte big-endian payload length, then the generated Protobuf
payload, bounded to 64 KiB. There are no Render envelopes or custom serializers.
Wrong directions, unknown types, oversized and truncated frames are rejected.

Every command has a positive increasing ID. Its named reply echoes that ID;
empty `error` means success. Commands apply in order at render boundaries,
and rejection preserves accepted state. There are no automatic retries.

`Metrics` reports completed frames, missed deadlines, queue depth, render duration,
active sequence and faults. FPS comes from frame-count and elapsed-time differences.
Both endpoints send ten-second heartbeats and time out after thirty seconds.
Unsolicited server heartbeats use ID zero; metrics have no command ID.
Final fault metrics are best-effort; exit status and stderr remain authoritative.

Radius owns audio watermarks and synchronized DMX playback. A command reply confirms
renderer application, not physical illumination.
