# Starfire naming review

Scope: important API concepts. Every reader was a fresh agent with no
inherited conversation, low reasoning and an explicit no-tools instruction.
The neutral names-only prompt came from the personal blind-naming-test skill.
Routine locals and protocol-standard terminology were excluded.

The measured input review is recorded separately in
[normalized RMS naming evidence](naming-review-audio-level.json).

## Output stages

Implementation: `RenderedFrame` in `starfire/src/engine.rs`; `render_at` composes
fixture properties and records them before software strobe gating. A separate
copy is gated and encoded into one full byte frame per universe. Replay hashes
and network output consume those final frames, not the semantic properties.

Reader `blind_starfire_frame_states`, names `RenderedFrame.states`:

> `RenderedFrame.states` likely holds a collection of states associated with one rendered frame.
>
> Ambiguities:
> - “States” does not identify what has state: objects, components, animation steps, rendering settings, or something else.
> - It is unclear whether these are states captured at rendering time, inputs used to render the frame, or changes produced during rendering.
> - The plural suggests a collection, but gives no clue whether it is a list, set, or mapping.

Mismatch: the distinction from encoded output was missing. Candidate tested by
`blind_starfire_composed_properties`:

> - `RenderedFrame.composed_fixture_properties`: Properties for one or more fixtures after combining multiple inputs or layers into a rendered result. “Composed” does not reveal the merge rules or whether these are final values. “Fixture” could mean a physical device, test data, or another domain object. The name also leaves the container shape unclear: properties of one fixture versus a mapping of fixtures to properties.
> - `RenderedFrame.universes`: A collection of universes associated with the frame. Without domain knowledge, “universe” is opaque. The name does not indicate whether the contents are identifiers, objects, grouped output data, or full snapshots.
>
> Neither contains an abbreviation. Both appear to be data attributes rather than actions.

Mismatch: final-stage distinction still absent. Next candidate, reader
`blind_starfire_output_stages`:

> - `RenderedFrame.fixture_properties_before_shutter`: Fixture properties captured before a shutter-related processing step. “Before shutter” is ambiguous: before applying shutter state, before opening or closing a shutter, or before a shutter event in time. The name does not establish whether these properties are raw inputs, intermediate results, or a snapshot.
> - `RenderedFrame.dmx_universes`: A collection of DMX universes associated with the rendered frame. “DMX” is an unexplained acronym; “universes” suggests separate address spaces or output groups but does not reveal whether the collection contains identifiers, configurations, or actual output values.

Mismatch: shutter could imply a physical event; output container remained opaque.
Final reader `blind_starfire_frame_contract`:

> - `RenderedFrame.fixture_properties_before_strobe`: Fixture properties captured before a strobe effect is applied. Ambiguous whether “before” means an earlier processing stage, an earlier moment in time, or values saved for restoration. The name does not reveal whether these properties are raw inputs or otherwise fully processed.
> - `RenderedFrame.dmx_frames_by_universe`: A mapping from DMX universe identifiers to DMX frames. “DMX” is unexplained, and “universe” appears to be a domain-specific grouping. The plural `frames` leaves unclear whether each universe maps to one frame or a collection of frames.

Adopted these final names. The prediction identifies both the pre-strobe stage
and universe-to-frame mapping. Typed fields and the struct comment resolve the
remaining stage/cardinality ambiguity. DMX/universe are protocol vocabulary,
not invented abbreviations. All renderer, replay, output and test callers changed.

## Override expiry

Implementation: `OverrideLifetime` in `starfire/src/model.rs`, `set_override`
and `ActiveOverride::expired` in `starfire/src/engine.rs`. Duration begins at
the command's supplied timestamp/beat. Held overrides survive sequence changes;
Clear, Stop and a script fault release them.

Reader `blind_starfire_override_lifetime`:

> - `OverrideLifetime::UntilCleared`: The override remains active until something explicitly clears it. Unclear whether replacement, reset, or shutdown counts as clearing.
> - `OverrideLifetime::Beats { duration_beats }`: The override expires after the specified number of beats. “Beats” needs domain context: musical beats, heartbeats, or periodic ticks. The name does not say when counting begins or how pauses affect it.
> - `OverrideLifetime::Seconds { duration_seconds }`: The override expires after the specified number of seconds. Unclear whether this is elapsed wall time or an active/running clock.
>
> The variants clearly communicate indefinite versus measured duration, and the fields make units explicit. There are no unclear abbreviations.

Kept: indefinite versus timed expiry and units align. Clock origin and Stop
semantics belong to the command contract; seconds are supplied session time,
never an ambient wall-clock read. Behavioral tests cover those distinctions.

## Sequence animation clock

Implementation: `ActiveSequence::render` subtracts its selection-time integrated
animation phase. `MusicalClock` integrates supplied elapsed seconds at tempo and
speed; speed changes preserve accumulated phase and beat corrections do not
jump that phase. This differs from musical beats elapsed since selection.

The lead identified `RenderContext.animation_beats` as missing origin and speed
semantics. First replacement candidate, reader `blind_starfire_elapsed_animation`:

> - `RenderContext.speed_scaled_elapsed_beats`: Elapsed time expressed in beats, adjusted by a speed multiplier. Unclear whether speed multiplies or divides the beat count, whether changing speed applies retroactively, and what event starts elapsed time.
> - `RenderContext.sequence_elapsed_beats`: Beats elapsed since the current sequence began. Unclear whether this resets when a sequence loops and whether playback speed affects it.
>
> Together, the names suggest two different time coordinates, but do not explain whether the first measures sequence time or time since some broader origin.

Mismatch: sequence origin missing. Shorter candidate, reader
`blind_starfire_sequence_phase`:

> - `RenderContext.sequence_speed_scaled_beats`: A beat quantity adjusted by the sequence’s playback speed. Unclear whether this is elapsed position, duration, or a per-frame increment; also unclear whether scaling multiplies or divides by speed.
> - `RenderContext.sequence_elapsed_beats`: Number of beats elapsed since the current sequence began, likely its current position. Unclear whether playback speed affects this count, and whether it resets on looping.
>
> Together, they suggest two timing representations, but the names do not establish whether the second is the unscaled counterpart of the first. No unclear abbreviations.

Mismatch: removing elapsed hid position versus increment. Final candidate,
reader `blind_starfire_sequence_elapsed`:

> - `RenderContext.sequence_elapsed_beats`: Time elapsed since a sequence began, measured in beats. Unclear whether this is raw musical time or already adjusted for playback speed, pauses, or looping.
> - `RenderContext.sequence_speed_scaled_elapsed_beats`: Elapsed sequence time in beats, adjusted by sequence speed. Suggests faster speed produces more elapsed beats for the same real time, but the scaling direction is not explicit. It also does not reveal whether changing speed integrates historical speeds or rescales all elapsed time using the current speed.
>
> Together, the names suggest an unscaled value and its speed-adjusted counterpart. No unclear abbreviations.

Adopted `sequence_speed_scaled_elapsed_beats`: sequence origin, elapsed position,
units and speed adjustment align. Integration versus retroactive multiplication
is explicitly documented and tested; spelling the algorithm into the field name
would add noise. Lua programs, runtime validation and tests use the new field.
Internal `ClockSample.animation_beats` remains a cumulative mathematical clock
coordinate, adjacent to its integration formula; this is a contextual exception,
not an additional blind pass.

Validation: Rust formatting, Clippy, all behavioral tests and unchanged sequence
DMX digests cover the renames. This is a bounded API review, not exhaustive naming
certification.
