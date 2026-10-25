# Starfire protocol

Autolux is the client; its owned Starfire process is the server. The single
[schema](../../proto/starfire/starfire.proto) generates Python and Rust bindings.
Every wire message states its direction. Application code owns bounded framing,
validation, rendering and process lifetime.

## Four command families

| Concept | Contract |
| --- | --- |
| `PlaySequence` | Play a bank-qualified Lua sequence until changed. The active identity continues without restarting. A different identity uses a cut or beat-duration crossfade. An absent sequence enters idle: clear the baseline and active effects and output darkness. |
| `SetActiveEffect` / `UnsetActiveEffect` | Set or remove an identified accent above the continuing baseline. Setting replaces its complete state, including lifetime. Removing an absent ID does nothing. |
| `UpdateBeat` | Supply fractional musical position and BPM. Starfire interpolates position between updates; animation speed is independent. |
| `SetParameter` | Change one defined, typed parameter. All other settings stay unchanged. |

Built-in parameters are generated typed alternatives, not string keys:

| Parameter | Type / range | Meaning |
| --- | --- | --- |
| `output_brightness` | Number, 0..1 | Final output multiplier. |
| `animation_speed` | Number, 0..8 | Animation rate; does not move musical phase. |
| `output_blackout` | Boolean | Final darkness gate, after all effects. |
| `audio_rms_normalized` | Number, 0..1 | Measured RMS, independent of operator brightness. |
| `sequence_parameter` | Active sequence, declared name and typed value | Change a control defined by that Lua program. |

The built-in value type is enforced by Protobuf's `oneof`. Lua declares custom
names, types, defaults and bounds; unknown names, stale sequence identities,
wrong types and invalid values are rejected. Missing measured RMS remains unknown.
Selecting the active sequence preserves its phase and parameters. Newly activated
sequences start with their declared defaults.

## Session and replies

`Login` contains the rig path, sequence directory, Art-Net destination, broadcast
choice and initial `SetParameter` values. Rust constructs one owning session,
validates initialization and then sends `LoginReply`. Failed initialization drops
partially acquired resources; the server remains idle until EOF or its startup
deadline, allowing a corrected login. The coordinator closes on a login error.
Duplicate login cannot replace a running session.
Closing the process pipe tears down the session and attempts fixture-aware darkness.

Each operation has its own reply: `PlaySequenceReply`, `SetActiveEffectReply`,
`UnsetActiveEffectReply`, `UpdateBeatReply` or `SetParameterReply`. An empty `error`
means success; a nonempty error explains rejection. `PlaySequenceReply` also gives
the active identity and its Lua parameter definitions. There is no generic command
result, separate stop operation or standalone parameter-catalog message.

Commands have positive, increasing IDs within the process session; replies carry
the corresponding ID and operation type. At each render boundary, commands apply
in received order against preceding accepted state. Validation rejection leaves
that state intact. A reply confirms renderer application, not physical illumination.
There are no automatic retries or parallel protocol versions.

`Metrics` reports elapsed time, completed frames, missed deadlines, command queue
depth, last render duration, active sequence and script/runtime faults. The dashboard
derives FPS from frame-count and elapsed-time differences. Heartbeats run every ten
seconds; thirty seconds without a response fails the session. Metrics may also be
sent unsolicited, using command ID zero. Server heartbeats may also use zero;
replies to client heartbeats carry that command's ID.

## Effect composition and time

- Targets are all fixtures, a named group or a fixture ID. Attributes and blend
  operations are enums. Ordinary effects cannot write raw reset or macro channels.
- Each attribute replaces a value or multiplies a continuous number. Multipliers
  are bounded to 0..8; colour uses replacement. Invalid targets, duplicate attributes
  and incompatible types reject the complete effect.
- Effects compose in ascending priority; higher priority wins, with command order
  breaking ties. Clamp each numeric result to its attribute range after each effect.
  Final brightness and blackout apply afterward.
- Lifetime is explicitly until removal, positive beats or positive seconds.
  Replacing an effect starts its new lifetime; removal reveals the continuing loop.
- Beat-duration expiry and crossfades use continuously accumulated musical beats.
  Tempo changes affect their rate; beat-phase corrections and animation speed do not.
  Seconds use explicit monotonic elapsed time.

Pipes use a four-byte big-endian length followed by Protobuf, bounded to 64 KiB.
Framing corruption, heartbeat expiry and fatal rendering faults produce darkness
and an explicit failure. Final fault metrics are best-effort; process exit and
stderr remain authoritative if the status pipe cannot flush. Radius owns audio watermarks and synchronized DMX playback;
this contract does not add a second media clock.
