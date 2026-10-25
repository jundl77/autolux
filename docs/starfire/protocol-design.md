# Starfire protocol proposal

Status: proposed v2 for discussion; the live system still uses v1.
The [schema](../../proto/starfire/v2/starfire.proto) is language-neutral. Standard
Protobuf generators produce the message types; application code owns validation,
composition and bounded stream framing.

## Stable operations

| Operation | Contract |
| --- | --- |
| Update beat clock | Supply fractional beat position and tempo. Musical time is independent of animation speed. |
| Ensure sequence playing | Select a bank-qualified sequence and loop it. The active identity continues unchanged; there is no restart operation. A different identity starts with the requested transition. |
| Patch parameter values | Atomically replace the supplied values on one target; omitted values remain unchanged. |
| Put active effect layer | Create or fully replace a named runtime layer, including its lifetime. The baseline sequence continues underneath. |
| Remove active effect layer | Remove that runtime layer; removing an absent ID is a no-op. It never deletes a Lua program. |
| Stop playback | Clear the baseline, its sequence parameters and active layers; release strobe and output darkness. Keep the process, beat clock, engine settings and analysis inputs alive. |
| Heartbeat | Maintain process liveness. Both directions retain ten-second heartbeats and a thirty-second timeout. |

## Parameters, not one command per knob

Three explicit targets separate engine controls, measured analysis inputs and
parameters owned by a particular sequence. A sequence target must match the
active bank/sequence; a stale control panel cannot change a different program.

| Target | Example registered key | Type and meaning |
| --- | --- | --- |
| Engine | `output.brightness` | Number, 0..1; final brightness multiplier. |
| Engine | `animation.speed` | Number, 0..8; animation rate, independent of beat phase. |
| Engine | `output.blackout` | Boolean; final output gate that every layer must obey. |
| Analysis inputs | `audio.rms_normalized` | Number, 0..1; measured RMS mapped to the documented input scale. |
| Sequence | `pulse_decay_beats` | Number; a Lua-declared parameter with its own default and bounds. |

Each parameter is registered with a type, default, unit and constraints. Lua
declares its own parameters; engine controls and analysis inputs have their own
registries. Catalog events expose those definitions to the existing dashboard.
Adding an input or a knob requires a definition and implementation, not a new
wire command. Numeric values must be finite; unknown keys, wrong types, duplicate
assignments and out-of-range values reject the entire patch without mutation.
Measured inputs have no invented default: absence remains unknown. Numeric bounds
retain the declared type, including exact integer bounds. A sequence's parameters
take their declared defaults when that sequence is newly activated; selecting
the already active identity preserves its values and phase. Engine settings persist.

Values are explicitly typed numbers, integers, booleans, text or RGB. There is
no arbitrary object tree or executable code in a parameter update. RGB channels
are normalized 0..1. Adding a new value type is a deliberate schema extension.

## Layers

A layer names its fixture selection, attribute assignments, priority and lifetime.
Fixture attributes use a registered vocabulary such as intensity, colour, pan,
tilt and strobe rate. Profiles define support and encoding; transport never exposes
reset, macro or raw-channel writes as ordinary attributes.

- An assignment either replaces a value or multiplies a continuous number. Integer,
  boolean, text and RGB attributes support replacement only in this version. Unsupported
  operations, unknown targets, duplicate attribute keys or incompatible attributes
  reject the whole layer. Registered attributes define their types, units, bounds
  and allowed operations. Multipliers are dimensionless and bounded to 0..8.
- Layers compose in ascending priority; higher priority is applied last. Ties use
  command order. Clamp numeric results to the attribute's valid range after each
  layer; nonfinite arithmetic is a rendering fault, never a DMX value. The final
  engine brightness and blackout gates apply afterward.
- Lifetime is explicitly until removal, a positive number of musical beats, or
  positive seconds. Replacing a layer begins its new lifetime at application.
- Beat durations and crossfades use continuously accumulated musical elapsed
  beats. Tempo changes affect their rate; phase corrections and animation speed
  do not jump or reset their progress. Seconds use monotonic elapsed time.
- Removing a layer reveals the continuing baseline; it does not restart it.

For example, a Drop accent puts `drop-arrival` on the wash group, replaces its
strobe-rate attribute for one second, then expires. A persistent red fixture uses
the same operation with a colour assignment and an until-removed lifetime.

## Ordering and failure

The owned process pipe remains a four-byte big-endian length followed by Protobuf,
with a 64 KiB bound. Each command has a positive, increasing ID within this process
session. Results correlate that ID and report application or rejection. There is
no automatic retry or duplicate-command replay; a replacement process starts a
new session with fresh state.

At each render boundary, validate and apply commands in received order against
the state produced by preceding accepted commands. For example, selecting B then
patching B in the same frame is valid. Each command is atomic; a rejected command
does not alter the state used by the next command.
Validation failures preserve the previous scene. Fatal protocol corruption,
heartbeat expiry or a Lua fault produces safe darkness and a reported failure.
Acknowledgement means the renderer applied the command, not that physical DMX
or light has been observed. Radius still owns audio watermarks and DMX playback.

The new package is v2; do not reinterpret v1 field numbers in place. Both sides
must verify the matching version before playback. A heartbeat receives a Health
response as well as its CommandResult; the process owner applies the existing
timeout in both directions.
This proposal changes no
live timing offsets. Exact source-time propagation remains a separate measured
timing improvement, not an assumed consequence of Protobuf.

## Naming review

Fresh readers, given identifiers only, distinguished clock updates, parameter
patches and composited layers. The first pass exposed ambiguity in `Control`,
selection versus playback, and partial updates versus replacement. Names now
identify a renderer command, ensuring playback, partial parameter patches and
active effect layers. `FixtureSelection` replaces the overly broad `target`.

Names alone cannot establish priority direction, ID scope, expiry rules or
idempotency. Those contracts are explicit above and require behavioral tests;
the blind review is a readability check, not a proof of protocol correctness.
