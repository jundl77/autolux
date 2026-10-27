# Starfire decision log

Status: prototype development on `starfire-prototype`. Every material decision
and unresolved question is recorded here for review. User decisions are binding;
prototype assumptions can change after an offline review and hardware audition.

## User decisions

| ID | Decision | Consequence |
| --- | --- | --- |
| U01 | Afterglow inspires the scripting API; the platform organizes fixtures, groups and banks. | Fixture-aware, composable effects; fixtures, groups, banks and looping autoloops. |
| U02 | Rust engine, Lua autoloops. | Embedded Lua; no JVM, Wasm or Clojure compatibility requirement. |
| U03 | Autolux selects named autoloops and transition policies. | Starfire executes commands; it does not classify music or select looks. |
| U04 | One autoloop corresponds to one Lua script. | Autoloops repeat until changed. Selecting the active autoloop is a no-op; no restart command. |
| U05 | Banks represent lighting categories. | Initial Atmospheric, Breakdown, Buildup and Drop banks; categories remain configuration. |
| U06 | Autolux supplies musical timing and explicit controls. | Beat/tempo/position/transport and controls; no PCM, energy bands or audio analysis in Starfire. |
| U07 | Radius owns timestamps and playback synchronization as implementation details. | Lua and the operator interface use musical time, never audio-frame watermarks. |
| U08 | Autolux controls accents and persistent overrides above the Lua baseline. | Strobe, brightness, speed and fixture colour can change independently of the autoloop. |
| U09 | Named fixture groups make autoloops portable between rigs. | Fixture setup maps roles such as wash and movers to actual hardware. |
| U10 | Keep script-failure handling simple. | No automatic bank failover or retry system. |
| U11 | Existing webapp gets fixture setup; keep one product entry point. | Root CLI coordinates components; no separate frontend. |
| U12 | Keep Art-Net output and the existing receiver network path initially. | Starfire emits Art-Net; Radius owns synchronized audio and lighting transport. |
| U13 | Every Lua autoloop has a deterministic beatgrid-to-DMX replay test. | Identical input, rig, controls and program produce identical DMX; live and replay share the renderer. |
| U14 | Work in an isolated worktree; commit meaningful slices to a branch. | Production master remains separate from prototype integration. |
| U15 | Delegate work, review it centrally and check progress every 15 minutes. | Three implementation workstreams; integration, decisions and final validation stay with the lead. |
| U18 | Bounded hardware auditions are authorized when the user is present. | Audio and lights were confirmed working; the isolated beat pulse was judged on-beat. Startup alignment is a separate requirement. |
| U19 | Use centered Control, Telemetry and Config tabs in the existing dashboard. | Control keeps essential performance, the timeline and horizontal diagnostics; detailed graphs live in Telemetry; Config owns fixture setup. |
| U20 | Keep one current protocol; overwrite it in place and commit directly to this branch. | Both implementations change together; Git provides history, with no parallel v2 schema or compatibility layer. |
| U21 | Use four command families: PlayAutoloop, active effects, UpdateBeat and SetParameter. | SetActiveEffect/UnsetActiveEffect share one concept. Parameters use defined enum types and values. |
| U22 | Initialization belongs in Login, confirmed by LoginReply; resources use RAII. | Login carries the rig, autoloops, output destination and initial parameters. The owned session releases resources on partial initialization and shutdown. |
| U23 | Use Metrics and heartbeats; denote direction on both shared protocols. | No separate Ready, StopPlayback, CommandResult or Health messages. Define client/server roles and directional comments in Starfire and Radius schemas. |
| U24 | Commands and replies are standalone messages, without Render envelopes. | A typed frame header identifies each generated message. Only first-order messages have direction annotations; Transition is nested in PlayAutoloop. Remove the custom Empty marker. |
| U25 | Parameters are global enum types plus values; keep the API simple. | Remove autoloop parameters, autoloop-reference objects, arbitrary names, blend operations and lifetime structures. Effects use direct properties and optional duration_seconds. Run focused checks for the final simplification. |
| U26 | Autolux owns fixture configuration and scripts; send everything in Login. | Typed rig, required profiles, Lua sources and initial settings initialize the session. LoginReply means ready; no paths, discovery handshake, RunConfig or OnReady. |
| U27 | Successful commands need no reply except Login. | ErrorReply identifies rejections; independent heartbeats show liveness and Metrics reports command progress. |
| U28 | Autoloop IDs identify playback independently of banks. | Banks group supplied scripts; PlayAutoloop uses a globally unique ID only. |
| U29 | Name the complete fixture setup RigDefinition and define it in rig.proto. | Login.rig contains fixtures, groups and their reusable DMX profiles. The name describes configuration rather than a running rig. starfire.proto imports the definition; both files share one generated contract. |
| U30 | Store shared rig files and fixture profiles in root rig/. | Autolux loads these assets into Login; native replay and validation use the same files. |
| U31 | Use autoloop throughout the API; name supplied programs AutoloopDefinition. | Login.autoloops carries each autoloop's ID, bank and Lua source; authored looks live in show/autoloops/. |
| U32 | Use GDTF as the basis for generic fixture definitions and retain rig.proto. | Prefer reusable open-source GDTF schemas and parsers where suitable. GDTF describes device types; RigDefinition describes their installed instances. Profiles carry standard GDTF XML and the selected DMX mode; existing open-source parsers compile physical controls at Login. |
| U33 | Radius follows the same protocol patterns as Starfire. | Standalone generated messages, Login/LoginReply, error-only replies, Metrics and independent heartbeats. Framing lives beside consumers. |
| U34 | Both Login messages carry protocol major/minor versions. | Major mismatch rejects before output acquisition; minor additions remain compatible and the reply returns the lower supported minor. |
| U35 | Enum definitions appear above messages. | Place them after package/import/options in every shared schema. |
| U36 | Starfire is the sole lighting engine and owns all lighting concepts. | Keep native replay, fixture and transport verification; remove obsolete external-control artifacts. |
| U37 | Define protocol versions in the schemas. | Consumers use generated major/minor constants; both protocols currently define 1.0. |
| U38 | Name Radius media messages AudioFrame and LightFrame. | Their message-type values are AUDIO_FRAME and LIGHT_FRAME. |


| U39 | Store authored autoloops in show/autoloops/. | Directory paths are plural; the autoloop API concept remains singular. |
| U40 | Use GDTF and MVR exclusively for fixture and rig assets. | Replace custom JSON fixture/patch storage; preserve standard archives and their resources. Implementation is pending. |
| U41 | Keep the physical rig independent of scripts. | Fixture selection belongs to show programming. MVR scene organization must not require Lua-specific role names. The selection API is under discussion. |
| U42 | Configure, save and load named fixture selections in the existing dashboard. | Store ordered, overlapping selections separately from the physical MVR scene. |
| U43 | Autolux generates one random run_id for Radius and Starfire at each start. | Use a nonzero random 64-bit value; reject Radius audio/light packets from another run and expose the same identity in renderer telemetry. |
| U44 | Keep the top Autolux title and place horizontal Control, Telemetry and Config tabs directly beside it. | No sidebar or centered navigation; remove decorative component/tagline labels. |
| U45 | Defer a Rust Radius rewrite and hosting TX inside Starfire on a separate thread/core. | Keep current process boundaries for this implementation; RX remains the remote output endpoint. |
| U46 | Replace Radius with Rust and embed TX in Starfire on a dedicated worker/core, superseding U45. | Keep Radius as a separate library boundary and RX as a remote executable; validate Rust audio and ENTTEC hardware early, then remove C++. All changes stay in PR 57. |
| U47 | Audio leads and lighting follows; use TCP and a 500 ms receiver buffer. | Audio references a cached lighting sequence. No reverse sequence dependency or application retransmission protocol. Longer delay belongs to analysis, not receiver synchronization. |
| U48 | Radius has no overlay functionality. | Starfire composes complete lighting states; Radius only transports, schedules and outputs them. |

## Prototype decisions

Starfire is the lighting backend. Its live contract is
`proto/starfire/starfire.proto`; output cadence is 40 Hz.

- P44: Use standalone Google Protobuf messages over the owned subprocess's
  stdin/stdout. Frame each with a generated MessageType byte and a four-byte
  big-endian payload length.
  Cap the Login payload, which carries the session assets, at 4 MiB and every
  other message at 4 KiB, checked by type before the payload is read. The root CLI owns
  launch, readiness and shutdown; no separately exposed control listener.
- P45: Apply accepted commands on the next 40 Hz render timestamp. The engine
  receives explicit time; monotonic pacing stays in the host. This preserves
  deterministic replay without assuming Python and Rust share a clock epoch.
  Art-Net ingress still samples DMX against Radius's audio watermark; exact
  target-watermark delivery remains a separate transport improvement.
- P46: Exchange renderer health and 10-second heartbeats; 30 seconds without
  controller input stops output. EOF, malformed input, queue overflow and fatal
  render errors attempt fixture-aware blackout and terminate the owned child.
  An empty PlayAutoloop enters idle without terminating the renderer.
- P47: Supply measured music energy separately from user master brightness.
  Normalize the analyser's smoothed RMS through a named -48 to -12 dBFS range,
  clamped to 0..1. This is measured loudness, not perceptual musical intensity.
  Missing measured energy stays unknown; programs explicitly choose their
  baseline brightness until a measurement arrives.
- P48: Remove the external channel-overlay protocol and mixer. Starfire owns
  all composition; Radius transports its complete DMX frames with audio.
- P49: Generate normal Python message classes and typing stubs with the official
  Protobuf compiler in build output; bundle them in wheels. Use the compiler
  bundled in build-only `grpcio-tools==1.62.3` for the installed Protobuf 4.25
  runtime. Rust uses prost generation. Build hooks stay in `tools/`; bounded
  process framing stays beside its CLI consumer. No dynamic descriptor loading.
- P50: The existing show-delay queue dispatches beat and measured energy
  together. Lua supplies the selected baseline look.
  Drop entry requests `drop_arrival`: 8 Hz strobe, one second, priority 100.
  A committed class change within the same lighting bank keeps the current
  autoloop and does not retrigger its entry accent. Persistent silence stops
  the renderer's look and discards queued commands that could reactivate it.
- P51: Reports name semantic autoloops and accents. Retain raw RMS and the
  original beat/section evidence. Compare those analysis invariants against the
  existing baseline without rewriting it. Validate changed lighting output with
  semantic command checks and deterministic DMX replay tests.
- P52: Keep the lighting beat ordinal monotonic for the full controller session;
  the analyser's per-song counter resets at silence and must not rewind Starfire.
  Audio level updates alongside detected beats and holds between them. A future
  continuous level stream also needs timestamped recording for replay parity.
- P53: Name the new measured control `normalized_rms_level`, with
  `AUDIO_RMS_NORMALIZED` and `rms_to_normalized_level`. Blind readers confused
  "music energy" with perceptual intensity and general "audio level" with volume
  control. The final name exposes the measured quantity and normalization;
  numerical mapping remains an explicit protocol/API contract.
- P54: Production rejects fixture universes that do not match Radius's single
  configured universe before starting the receiver or audio. Standalone Starfire
  retains multiple-universe support; silently dropping patched fixtures is invalid.
- P55: Refresh full-report benchmark checksums only after reproducing the old
  baseline and proving all ten tracks retain identical raw beat, section and
  sound events, scores and counts. Verify each lighting/schema difference against
  the declared behavior. Keep comparison gates and every other baseline field.
- P56: Report replay performance as represented show seconds divided by elapsed
  rendering seconds. Include command application, Lua, composition and encoding;
  exclude file parsing/output and network pacing. Default tests render a mixed
  90-second show: minimum 10x real time for the room and 2x for a
  64-fixture patch. These are conservative regression floors, not hard deadlines.
- P57: Use nonblocking Art-Net sends instead of a 100 ms socket timeout on the
  render thread. A send failure retains the existing fail-closed policy and
  attempts blackout. Do not add triple buffering or a backlog of completed DMX
  frames; consider a latest-frame handoff only if output separation is measured
  to be necessary. Radius's intentional synchronized playback buffer is separate.

- P59: Run the standalone dashboard synchronously from the CLI main thread.
  Its blocking server and mDNS registration must not occupy an asyncio event
  loop or an executor worker that prevents bounded Ctrl-C shutdown.
- P60: The room patch uses two Spot 110 movers, four SePar washes and eight
  CF805 RGB cells at addresses 97, 100, ... 118 in its verified 24-channel mode.
  Mini Kinta channels 40..43 stay dark until its profile is verified. The Spot
  shutter value 7 and blue-wheel value 111 lie within documented open/blue ranges.

- P62: Round fixture encoding to the nearest DMX value. Dimmer levels and
  movement coordinates remain semantic script values; named groups keep programs
  independent of patched addresses.

- P64: Readiness must execute each registered Lua program once against the
  selected fixture patch, with no network output, and reject script/group errors
  before starting Radius. Parsing a rig and listing filenames cannot detect an
  incompatible saved patch. This is a startup probe; replay tests cover the
  programs' later branches. Preserve custom saved patches.

- P66: Drop's rhythmic brightness envelope must keep running when animation
  speed is zero. Gate its initial hold with unscaled musical elapsed time, and
  align the authored envelope to the musical bar containing autoloop selection.
  Keep movement and colour on the continuous speed-scaled autoloop clock.
  Test nonzero selection positions, beat corrections and animation freeze at
  entry and loop boundaries. A global 64-beat envelope plus a relative hold can
  otherwise add an unintended second hold near a global phrase boundary.
- P67: Initialize analysis models before starting Radius capture. Model loading
  must not give room audio a head start over lighting. Preserve bounded startup,
  partial-start cleanup and receiver ownership; readiness checks remain output-free.
- P68: Show measured Starfire frame rate from render-counter and renderer-time
  deltas. Missing, stale and restarted measurements must not appear as healthy
  zeros. Distinguish renderer FPS, host USB writes and physical DMX refresh.
- P69: Diagnose musical alignment with a single white pulse on the wash and
  strip, using absolute beat phase, a cubic fade and a 65% peak. Disable movement,
  section-dependent looks and accents in the temporary audition. Verify the
  envelope and deterministic DMX replay before physical output. Do not apply a
  guessed phase offset after the user confirms the pulse is on-beat.
- P70: Maintain one current Starfire schema with standalone operations and
  global enum parameter types plus validated values. Effects use direct fixture
  properties. See [protocol](protocol-design.md).
- P71: Effect expiry uses elapsed seconds; crossfade duration uses accumulated
  musical beats, independent of beat-phase corrections and animation speed. Apply
  queued commands in order against the result of preceding accepted commands;
  reject invalid commands without changing accepted state.
- P72: An empty autoloop ID in PlayAutoloop enters idle and clears the baseline
  and active effects. This expresses the idle playback state without a separate
  stop message. Idle rejects
  a transition policy, while same-identity play validates but never restarts.
- P73: Only Login has a success reply. ErrorReply carries a rejected command's
  ID and reason. Metrics.last_processed_command_id includes accepted and rejected
  commands, in input order. Independent heartbeats have no ID or echo. A stalled
  progress counter cannot be kept healthy by continued metrics or heartbeats.
- P74: A failed Login releases partially acquired resources and replies with an
  error. It remains idle under the original startup deadline until corrected or
  disconnected; production closes immediately on that error. A second Login
  cannot replace a successfully initialized session.
- P75: Keep frame dispatch small: MessageType comes from the shared schema,
  payload serialization is generated, and runtime code handles only framing and
  typed dispatch. Zero-length bodies are valid for unsolicited heartbeats/metrics.
  Unknown types, wrong directions, oversized and truncated frames are rejected.
- P76: Shared Parameter enum/value pairs initialize Login and update global
  runtime state through SetParameter. Cut is PlayAutoloop's default; its nested
  Transition has an optional positive crossfade duration. Effect properties are
  literal overrides; optional duration_seconds replaces a lifetime object.
  Omitted effect target means all fixtures. No generic Empty marker is necessary.
- P77: Load the controller's patch, used profiles and Lua sources before launching
  Starfire. Send this immutable snapshot through typed Login fields. Keep the
  4 MiB Login bound (64 KiB per Lua source). Oversized input fails before launch; GDTF XML and Lua
  source share the same session budget.
- P78: Validate all supplied scripts with initial parameters in an output-free
  probe engine, then discard it. The real session begins pristine; script probes
  must not leak movement positions, Lua state or selection into idle. Acquire
  unicast Art-Net output only after validation. Asset edits apply on a new session.

| ID | Decision and reason | Alternative / review point |
| --- | --- | --- |
| P01 | Install pinned Rust 1.99.1 MSVC, rustfmt and Clippy; use the existing VS 2022 C++ build tools. | Pin updates deliberately after validation. |
| P02 | Use embedded Lua 5.4 through `mlua`, with vendored Lua and explicit standard-library access. | Lua implementation can change without changing fixture semantics. |
| P03 | One Rust crate with separate timing, autoloop, composition, fixture and output modules. | Avoid a framework, plugin graph or ECS before a concrete need. |
| P04 | JSON replay streams and live Protobuf commands use the same renderer. | Shared schemas live under root `proto/`; generate language bindings. |
| P05 | Render at 40 Hz by default; use supplied musical time rather than frame counts. | Test other cadences; no claim of sample-exact physical lighting. |
| P06 | Semantic intensity/colour/pan/tilt/strobe properties are composed before fixture encoding. | No raw-channel writing in normal autoloops. |
| P07 | Overrides have explicit targets, identities, priorities and lifetimes; highest priority then latest command wins conflicting absolute properties. | Manual versus automatic priority defaults need user review (Q02). |
| P08 | Literal property overrides compose with the baseline; global brightness and blackout always apply last. | Absolute intensity remains available for persistent illumination. |
| P09 | Speed changes an integrated animation-beat clock; the musical beat clock stays unchanged. | Initial speed is global; per-group speed can follow a concrete use case. |
| P10 | A script fault stops lighting, releases strobe and outputs fixture-aware blackout; audio remains independent. | No automatic replacement; asset changes require a new session. |
| P11 | Start with verified Spot 110 and SePar profiles plus generic RGB/RGBW/dimmer fixtures. | Mini Kinta stays dark until its mode/profile is verified (Q04). |
| P12 | All overnight validation is offline or UDP loopback. | Real fixture colour, movement and timing remain a later audition; never wake the room with test output. |
| P13 | Each autoloop has a replay fixture and a checked full-frame DMX digest, plus semantic assertions. | Golden hashes alone are insufficient; assert expected light behaviour as well. |
| P14 | Reuse the existing Python environment explicitly without installing an editable worktree over production. | Tests verify imports resolve to the worktree; no duplicate dataset/model copies. |
| P15 | Autolux supplies a validated GDTF profile catalogue; rig files reference profile IDs. Preserve patch/group order. | Encoding performs no filesystem I/O. Verify nominal strobe-rate facts against manufacturer documentation. |
| P16 | Lua returns semantic effects using `sf.scene`, `sf.dimmer`, `sf.colour`, `sf.pan_tilt`, groups and oscillators. | Authors can calculate their own curves; helpers do not limit creative effects. |
| P17 | Bound each Lua VM to 16 MiB, 100,000 instructions per load/render, 128 KiB source and 512 effects per frame. | Named prototype limits; measure real autoloops and adjust explicitly. No ambient clocks, I/O, imports or unseeded randomness. |
| P18 | Clock integration changes on beat/control events, not on rendered frames. | Replay cadence cannot change integrated animation speed. Beat-anchor corrections are distinct from speed changes. |
| P19 | Crossfade continuous semantic properties; switch discrete strobe to incoming value at fade completion. | Avoid invalid intermediate shutter functions. An interrupted fade captures its current baseline, bounding active VMs. |
| P20 | A fault retains the selected autoloop identity in stopped state; duplicate selection cannot retry it. | Explicit selection of a different autoloop recovers. Fault output clears overrides/strobe and preserves last valid movement. |
| P21 | Global animation speed accepts 0..8; normalized brightness accepts 0..1. | Named prototype bounds, separate from device-specific movement/strobe limits. |
| P22 | Implement strobe as deterministic intensity gating in the renderer; leave hardware shutter in its verified open range. Limit to 10 Hz at default 40 Hz rendering. | Manufacturer channel ranges do not establish calibrated Hz transfer curves for every fixture. Four samples/cycle improves representability; physical fidelity still needs audition. |
| P23 | Keep semantic requested state for diagnostics; encode a separate shutter-gated state. | Prevents diagnostics from confusing the requested strobe with a hardware rate byte. |
| P24 | Art-Net output is an explicit-destination ArtDmx adapter; support the user's existing broadcast path as an explicit compatibility option. | It is not a full Art-Net discovery/subscription node; default output is offline and tests use loopback. |
| P25 | Remove Lua `pcall`/`xpcall`, coroutines and debug access from authored autoloops. | Scripts must not catch and indefinitely retry an instruction-budget failure. Catalogue limits: 256 autoloops/4 MiB total; at most 512 resolved targets per render. |
| P26 | Determinism covers identical scripts, rig, events and render cadence. Bundled autoloops derive animation from supplied time and also pass cadence-invariance checks. | Arbitrary Lua can retain closure state; document pure time-based authoring instead of recreating a VM every frame. |
| P27 | RGBW extracts the neutral RGB component; colour wheels use nearest normalized swatch and apply colour magnitude through the dimmer. | Physical colour balance remains subject to audition. Movement bounds and inversion are rig calibration. |
| P28 | Paced replay drops stale frames instead of bursting them after a stall. Completion, interruption and send failure attempt fixture-aware blackout at the last emitted positions. | UDP cannot guarantee delivery; receiver watchdog remains necessary. Use the standard `ctrlc` host handler, separate from the deterministic engine. |
| P29 | The first fixture tab edits and validates the patch only; audition and master controls remain CLI commands until a live renderer session exists. | Do not display inactive controls or create a second frontend. |
| P30 | Convert reports to musical source time: beat `t`, intent `song_t`, falling back to intent `t` minus the recorded look-ahead once. | Simulation can clamp delayed delivery at EOF. This is intended musical replay, not a faithful record of delivery lateness. Reports omit accents and downbeat ordinals; do not invent them. |
| P31 | Fixture edits validate through the Rust encoder before atomic replacement; revision checks reject stale browser edits. Changes apply on the next session. | Preserve group member ordering; no patch hot reload in the prototype. |
| P32 | Build checks run rustfmt, locked Clippy/tests and release compilation. The short production readiness check remains separate. | Do not add long suites to `--prod --check`. |
| P33 | Explicit network playback requires at least four frames per requested strobe cycle. | Lower-rate offline replays are sampling diagnostics; reject aliased physical strobe output. |
| P34 | Report conversion infers beat ordinals from detections, skips zero-tempo warm-up and holds the last positive tempo afterward. Silence and EOF enter idle. | Reports do not preserve true bar origin; exact beat/downbeat phase requires a richer live capture contract. |
| P35 | Root CLI requests blackout using an owned stop-file signal before bounded child shutdown. Native Ctrl-C remains available. | Windows headless children cannot reliably receive console signals; force termination remains the final timeout fallback. |
| P36 | Bound rig input to 1 MiB, fixture/group counts to 512 and identifiers to 128 bytes. Bound latched diagnostics to 1 KiB and replay diagnostic storage to 8 MiB. | Lua memory limits alone do not bound host copies of errors or rig identifiers; adversarial resource regressions cover these boundaries. |
| P37 | Replay defaults to the dashboard's saved rig when present, otherwise the committed room rig; explicit paths win. | A custom fixture-store path requires the matching explicit replay rig. |
| P38 | Beat corrections update authoritative musical position; animation phase integrates elapsed tempo/speed continuously and does not jump with corrections. | Speed-scaled phase corrections could make animation negative and visibly restart it. Authors can use beat_position for strict beat phase; a phase-lock/slew policy is a future review question. |
| P39 | Blind naming review covers output stages, override lifetimes and the two autoloop clocks. Rename ambiguous public frame fields and make the Lua speed-clock origin/units explicit. | See the naming review for exact reader predictions and retained contextual ambiguities; no exhaustive identifier pass. |
| P40 | Buildup and Drop intensity oscillators follow absolute musical beat position, including phase corrections and selection between beats. Movement and colour use the continuous speed-scaled autoloop clock. | A free-running animation clock can drift from detected beats; an autoloop-relative origin can offset every pulse after a mid-beat selection. Speed controls those animations without desynchronizing intensity from musical beats. |
| P41 | Fixture rows retain their original identity during edits so renaming preserves ordered group membership. | Group order drives spatial phase; treating a renamed fixture as a new member silently changes choreography. |
| P42 | Reject capture/summary paths that collide with replay input, rig input or each other before writing. | Explicit output paths must not accidentally destroy the audition inputs or replace a binary capture with its summary. |
| P43 | Exclude Lua pointer/identity formatting from the authoring environment. | `tostring` on tables/functions and pointer-capable `string.format` expose allocator addresses, an ambient source of nondeterministic effects. Autoloop math and ordinary string concatenation do not need them. |

- P79: Carry canonical GDTF description.xml plus an exact mode in FixtureProfile.
  No reusable GDTF Protobuf schema was found in the scoped official/parser repositories. Reuse pygdtf 1.4.5 for imports and gdtf 0.3 for Rust parsing; avoid a second handwritten XML schema. All five bundled definitions validate against the official XSD.

- P80: Compile imported profiles once at Login; explicit unsupported constructs reject before output.
  Support direct geometry, physical channel mappings, 8/16/24/32-bit values, named presets and supported ModeMaster dependencies. Geometry references, virtual/multiple-break channels, relations and nonlinear/time-dependent brightness need later implementations. Do not silently approximate them.

- P81: Import .gdtf through a bounded, atomic CLI workflow.
  Select an exact mode, validate a staged catalogue with the real renderer, then atomically publish. Replacing a profile ID applies next session and requires no native rebuild. Complete XML is preserved; --profiles flows through replay, production and fixture setup.

- P82: Keep checked adapters for two upstream parser defects.
  Materialize omitted Highlight=None before parsing, and implement checked standard DMX value byte mirroring/shift using parsed accessors. Regression cases preserve valid imports and reject values beyond their declared resolution.

- P83: Radius retries identical Login every 250 ms for at most 15 seconds.
  UDP loss cannot strand startup. Repeated Login replies without resetting resources; changed initialization rejects. Receiver listener readiness precedes hardware-ready LoginReply. Source IP/session/first accepted source port route a session; this is not cryptographic authentication.

- P84: Schema enums define major 1, minor 0; use generated constants and negotiate min(client, server).
  Major compatibility is explicit. Message enums precede messages; only wire messages carry direction comments. Preserve bounded framing and generated language bindings.

- P85: Keep GDTF physical controls distinct from fixed global parameters.
  Lua physical_attribute/channel_preset and SetActiveEffect.attributes target fixture capabilities. SetParameter remains the closed global enum. Unknown/ambiguous properties, unsafe brightness mappings and inactive requested functions reject.

- P86: Production gives Starfire a private loopback Art-Net destination on a
  fresh UDP port, exclusively bound by Radius. Standalone Art-Net retains 6454.
  Standard ArtDmx has no run identifier: shared ingress could otherwise stamp
  unrelated traffic with the current run_id. A port-claim race fails startup.

- P87: Keep automatic lighting dark until sound has started and its delayed
  room-playback deadline is reached. Silence predictions cannot select a loop or
  trigger an accent. A silence cutoff invalidates pending intent callbacks;
  preserve the existing two-second persistent-silence cutoff policy.

- P88: Starfire accepts one nonzero run_id at Login and returns it in LoginReply
  and Metrics. Its owned process stream scopes subsequent commands to that run;
  no repeated identifier is needed on every command. Radius carries run_id on
  every datagram. This identity is separate from replay seeds and media timing,
  and is not network authentication.

- P89: Start an atmospheric baseline at the delayed room-start deadline if no
  committed intent is due. Preserve future scheduled intents and skip the
  baseline when a committed section is already ready. Startup must not wait for
  classifier warmup after audible music begins.

- P90: Surface audio backend failures with their descriptive error and preserve
  the cause. Windows session/device changes can invalidate both capture clients;
  do not mislabel this as buffer overflow or silently switch audio devices.
  Recovery currently requires starting a fresh run after the device stabilizes.

- P91: Resolve GDTF function bounds numerically against currently active
  ModeMaster branches, independent of XML order. Function-valued ModeMaster
  references must themselves be active and contain the selected DMX value.
  Sort ChannelSets and reject presets outside the effective function range.
  Preserve authored InitialFunction defaults. This follows the maintainer's
  clarification in [specification issue 103](https://github.com/mvrdevelopment/spec/issues/103);
  the specification wording remains unresolved.

- P92: Use CPAL/WASAPI for Rust audio and the safe libftd2xx binding for the
  existing ENTTEC USB Pro protocol. Keep audio callbacks bounded and free of
  allocation, network and USB operations. USB remains on a separate worker;
  driver bindings do not require retaining application C++.

- P93: Radius uses one ordered duplex TCP stream with bounded protobuf records.
  Send each changed lighting state before audio referencing its sequence. RX
  resolves that state into the admitted playback block; network arrival never
  advances physical lighting. No reverse dependency or custom ACK/retry.
- P94: Fill/refill 500 ms of audio at RX; consume valid buffered audio before
  silence and blackout. Bounded queues fail explicitly on overflow. CPAL's
  audible-source clock accounts for output-device latency and starvation tails.
- P95: Associate delayed audio with lighting at block assembly time, retaining
  that association during TCP backpressure. Initial show-delay fill transmits
  silence. Keep the 14-second analysis lead separate from receiver buffering.
- P96: Starfire owns the TX worker by RAII; CLI owns Starfire and remote RX.
  Network, render and DMX workers support explicit CPU affinity. CPAL/Windows
  schedule audio callbacks; no ignored callback-affinity configuration remains.
- P97: Receiver `ready` means its TCP listener is available. Radius LoginReply
  means physical outputs are initialized; Starfire LoginReply includes that
  readiness. Remote preflight deploys/checks without starting playback.
- P98: Remove all application C++ and its build/vendor dependencies after the
  successful Rust hardware proof. Build both crates from one Cargo workspace.
  A generated ignored legacy build directory remains locally because automatic
  approval review blocked its deletion; it is not part of the repository.

- P99: Keep Starfire and Radius protobuf contracts independent. Starfire may use
  the Radius Rust library, but its schema must not import Radius messages.
  Radius writes its transport-health snapshot; Starfire emits renderer Metrics.
  Autolux combines both for the existing dashboard. This supersedes the short-lived
  cross-protocol metrics import; no duplicated transport message definitions.

- P100: The rig is an MVR 1.6 file with its GDTF files embedded
  ([rig-mvr.md](rig-mvr.md)). Autolux validates and parses it into the existing
  Login rig; offline Starfire commands read that exported `RigDefinition`, so there is
  one MVR parser and one native validator. Moving-head homes derive
  from the fixture Matrix, superseding the `pan_offset_degrees` fields. The
  `--profiles` catalogue is only the fixture library for import and Config; it no
  longer flows through replay, production or Login (supersedes P81's last clause).
  2026-10-26: the library is `rig/library/`, real GDTF 1.2 archives named by their
  GDTFSpec, replacing `rig/profiles.json` (XML in JSON); import installs the archive as is
  and `--library` replaces `--profiles`.

## Open questions and defaults

| ID | Question | Prototype default |
| --- | --- | --- |
| Q01 | When should a valid Lua edit activate? | Start a fresh replay after editing in the prototype. Live reload should preserve musical position; explicit versus next-bar activation remains open. No restart command. |
| Q02 | Should persistent manual colour overrides win over automatic accents? | Explicit priority; manual defaults above automatic on the same property. Unrelated intensity/strobe controls still combine. |
| Q03 | Named calibrated movement targets or direct pan/tilt first? | Direct normalized pan/tilt with calibrated bounds; API leaves room for named positions without needing a 3D editor. |
| Q04 | What is the verified Mini Kinta IRC control profile? | Keep it dark. CF805's saved 24-channel mode is verified and represented by eight RGB cells at addresses 97-120. |
| Q05 | Which manual controls belong in the first webapp tab? | Patch/groups now; live audition, master and blackout follow the live-session integration. No browser code editor. |
| Q06 | Where does Art-Net sit? | Standalone renderer output only. Production passes complete DMX through the embedded Radius Rust API. |
| Q07 | Are multiple universes needed immediately? | Encoder/output support universe identity; current Radius deployment remains its existing configured universe. |
| Q08 | How should exact beat source times and physical-output latency be measured? | Preserve the successful pulse audition as evidence, not a sample-exact guarantee. Beat timestamps currently lose batching age; USB cadence and fixture response remain outside command-delay metrics. Measure these before adding compensation. |

## References and what to borrow

- [Afterglow](https://github.com/Deep-Symmetry/afterglow): semantic effects,
  dynamic parameters, metronome snapshots, colour and movement abstractions.
- [BlinderKitten effects](https://norbertrostaing.gitbook.io/blinderkitten/object-types/effects):
  relative/absolute effects, speed, fixture spread and layering.
- [QLC+ fixture manager](https://github.com/mcallegari/qlcplus-docs/blob/main/pages/04.fixture-manager/chapter.v4.md):
  patching, fixture modes and groups; existing local profiles supply channel facts.
- [Chataigne](https://github.com/benkuper/Chataigne): typed control values,
  parameter recording/replay and separation between controller and renderer.
- [Pharos Lua guide](https://dl.pharoscontrols.com/documentation/application_notes/Pharos_Scripting_Guide.pdf):
  a focused Lua host API for show control, rather than exposing device internals.

These are design references, not newly adopted runtime dependencies.

## API and composition details

Lua provides the baseline. Autolux can set both expiring accents and persistent
overrides on named groups or individual fixtures. An override changes only its
specified properties: holding one fixture red preserves its baseline movement
and brightness unless those are also overridden. Clearing the override reveals
the continuing baseline; it never restarts an autoloop. Held overrides survive
autoloop changes until cleared or stopped. Rust produces one composed DMX frame.

Speed is a supplied animation control, not a transformation of already encoded
DMX. Integrate animation beats continuously through speed changes while keeping
the musical beatgrid unchanged. Per-group speed remains a later extension.

The fixtures tab maps named roles to concrete fixtures. Autoloop identity is a
globally unique ID supplied with source at login. Banks are grouping metadata;
playback never accepts a server-side file path.

## Review checkpoint

2026-10-25: Native renderer, fixture, protocol and transport checks remain the
validation contract. User decision U36 removes external-control artifacts and
obsolete implementation plans; authored autoloops keep their native replay tests.


Pending standards work: preserve complete GDTF/MVR archives, extend break patching,
geometry references, channel functions, virtual controls/relations and physical
mappings. Carrying XML preserves data but does not imply full rendering support.
The proposed use of MVR UserData for Lua selections is superseded by U41; no
application-specific rig extension is approved. Movement restrictions remain an
independent control-policy question.

Optional experiment drivers require explicitly selected source/interpreter paths,
with provenance guards. Removing obsolete report metadata preserves all eight
gated analysis invariants for each of the three recorded digest tracks.

2026-10-25 review hardening: remove native Lua pattern matching because instruction
hooks cannot bound its backtracking. Treat fixture encoding failures like other
script faults: publish safe darkness and a bounded diagnostic. Accepted GDTF
emitters must obey blackout and brightness, including white-only fixtures and
nonzero profile defaults. Additional emitters default dark until explicitly
controlled; white-only fixtures use colour brightness without requiring RGB.
Radius health-file failures are diagnostic failures,
not a reason to stop audio or DMX; retry with throttled logging.

2026-10-25: Confirmed source silence follows the delayed audio boundary; pending
beats and section decisions remain queued. Detector beat positions carry detector
tempo, while the analysis/display tempo may be octave-folded.

2026-10-25 user decision: manual annotations override published labels for tracks
already admitted to training. Preserve split membership and evaluation exclusions.
Simulation and report fingerprints use the same explicitly selected corpus/model root.

Fixture configuration uses a library, patch list and 512-channel map. Display
universes from 1, retain native addressing internally, and validate saves atomically.

2026-10-26 user decision: the palette is a live layer. Autolux owns the palette
catalogue and sends it in Login (first entry active). Starfire keeps one active
palette render state; `PlayAutoloop.palette` (required) retargets it with the loop's
transition and the `PALETTE` parameter retargets it over the default crossfade.
`sf.palette()` reads that state; `sf.palette(name)` reads a named table entry.
Starfire has no built-in palettes: replays embed the table and `check` takes --palettes.
Scripts see read-only palette tables, so slots cannot detach from the per-frame refresh.

2026-10-26 intensity macro: one operator `INTENSITY` parameter (0..1) scales how
hard looks hit without changing what they do. Autolux defaults to 0.7 for living-room
shows and persists it in show settings; Starfire defaults to 1.0, which renders
authored loops exactly as written, so goldens replay at 1.0. Pulses multiply
`sf.on_beat` by `lerp(0.35, 1, i)` and widen its decay to `width * (2 - i)`.
Strobes share one 0.3 gate in Starfire and Autolux: below it no strobe renders and
no drop accent is sent; above it the rate and burst caps scale by `i`, and the drop
accent cooldown divides by `i`. Pan/tilt slew at most `0.6 / (1 - i)^2` of the
normalised range per second, unlimited at 1.0. The selection policy caps energy at
tier 1 below 0.35 and tier 2 below 0.7, then applies the nearest-tier fallback.

2026-10-26 specials: `SetActiveEffect.special_fx` (a `SpecialFX`) renders a
fixture-agnostic accent on its target group at the message priority, keyed only on GDTF
attribute families.
Colour slot: active palette colour, then ColorAdd/ColorSub mixing or the nearest
coloured Color(n) wheel slot. Energy: semantic intensity (Dimmer, mixing gain, dark
wheel slot). Speed: `*Rotate`/`*WheelSpin` over their physical range (from zero when
the range is bidirectional), else a pan sweep centred on the beam. Pulse, on the bar
grid: advance a Gobo(n) wheel, else a Color(n) wheel when the fixture cannot match
colour, else gate a continuous Shutter(n) between its channel default (open) and
PhysicalFrom (closed), else gate to a zero-luminance wheel slot. A plain dimmer blink is
not a pulse. Shutter(n)Strobe is not used: its Hz free-runs off the beat grid and
bypasses the software strobe caps. Pulses never exceed the strobe rate cap times the
intensity strobe scale (the period lengthens by whole multiples); gating pulses also
stop after the strobe burst cap, slot advances continue. Pattern selects a Gobo(n),
else Prism(n), slot. Attack/release ease on elapsed musical beats; release runs when
the effect is cleared, replaced or expires. Speed scales by intensity continuously
(Starfire only; energy is never scaled by intensity) and the pan sweep accumulates its
phase per frame, so intensity changes bend it without jumping; only pulse stops below
the strobe gate. Whether a special is intense stays Autolux policy. An unknown colour
slot leaves colour to the look. A group absent from the rig renders nothing. When two
attribute keys address one DMX channel, the later (higher-priority) layer wins and the
earlier key is dropped for that channel; this is not an error. The rule holds between
layers only: two such keys inside one look stay a conflicting-GDTF-values fault, because
that is a script authoring error. LoginReply reports per
group which axes at least one member can express, and login fails if that reply
exceeds the real-time message cap.
A dimmerless wheel resolves to its zero-luminance slot at zero intensity or black
colour; without such a slot it closes its continuous Shutter(n) instead.

2026-10-27 cells: the addressing unit is the cell, not the fixture. A fixture contributes
one cell per channel-owning geometry of its mode; a geometry above exactly one
channel-owning leaf joins that leaf's cell (a mover's yoke and head), one above several
leaves is a cell of its own (a bar's master dimmer). Cells order by first DMX offset. A
single-cell fixture's cell keeps the fixture ID, other cells are `fixture/geometry`; a
fixture target on a multi-cell fixture means all its cells. Groups list fixtures and expand
to their cells. Each cell encodes its own channels from its own state; ModeMaster across
cells is refused. The rig proto declares each fixture's cells as Autolux derives them, and
Login fails if Starfire derives different ones. GeometryReference stays unsupported.
A cell whose own geometry carries a Dimmer above other cells is a master: GDTF multiplies
its output into every pixel below it. It is not a group member; Starfire sets its level M
to the largest light any of its pixels asks for (intensity, or attribute brightness when the
pixel has explicit attributes) and divides M out of each pixel's intensity and attribute
brightness, so M times the pixel equals the fixture without a master. Its own state's
intensity is ignored; nested masters are refused.

2026-10-27 GDTF compiler: Autolux compiles each patched mode with pygdtf 1.4.5
(`autolux/lighting/gdtf_compile.py`) into the typed `DmxMode` of rig.proto; Starfire
validates and encodes from it and has no GDTF parser (the gdtf and quick-xml crates are
gone; this supersedes U32's "existing parsers compile at Login" and the gdtf 0.3 note).
pygdtf drops DMXValue's byte-shifting flag, range checks and mirroring, so the compiler
re-reads DMXFrom, Default, ModeFrom, ModeTo and set physical values from description.xml.
Starfire's tests build modes directly; the synthetic test descriptions compile into the
committed `starfire/tests/data/compiled_profiles.pb` (`tools/compile_test_profiles.py`).

2026-10-27 MVR reading: `mvr.patched_fixtures` reads through pymvr 1.0.7 and `mvr.gdtf_modes`
through pygdtf (GeometryReference breaks expanded). pymvr re-decodes GDTFSpec as cp437 and
maps an unreadable address to DMX 1, so the spec text and address validity come from the
scene XML. Our writer, editor tree and schema validation stay: pymvr's writer reorders
elements and drops nodes it does not model, which Config's byte-for-byte round trip forbids.
A room.mvr written by pymvr reads back to the same patch and exports the same rig.

2026-10-27 GDTF coverage (bounded): the compiler expands GeometryReference instances at
their Break offsets, leaves virtual channels out (they carry no DMX; a ModeMaster on one is
refused), takes GDTF 1.0's channel Default when there is no InitialFunction, names an
unnamed function `Attribute n` for paths and ModeMaster links, ignores a WheelSlotIndex on a
function without a wheel, and drops channel sets that have neither a physical range nor a
wheel slot (GDTF Builder's min/max labels; the function's own mapping encodes the same
DMX). DMXProfile curves are carried as points and inverted by Starfire on semantic and
physical values; x and the output are fractions 0..1 of the DMX and physical range (GDTF
1.2 Figure 1 sets "DMXPercentage = 0,75" for 75% of the DMX range, and its curves reach 1 at
full DMX), points outside 0..1 are refused, and so are non-monotonic curves. Master and
attribute brightness still scale an explicit value on a curved channel in DMX steps. When
several active functions on one channel can express the look, the lowest DMX range wins.
Still refused, each with its reason (pinned per corpus mode in test_gdtf_compile.py):
Relations, sub-channel sets, wheel Filter slots, DMX breaks other than 1, GDTF versions
outside 1.0..1.2, duplicate function names, and brightness functions that share their
channel, carry presets, depend on a ModeMaster or run inverted.

2026-10-27 Spatial group order: a rig group (MVR Class) lists its members left to right by
the X of each fixture's placed MVR Matrix (scene space, layers and parents composed); equal X
keeps file order, and a group whose members all share one X keeps file order with a warning
naming the group. The exported RigDefinition carries that order, so `sf.group`/`sf.members`
in Lua see it unchanged. Mirror pairs are by symmetry about the group's centre in that order:
`sf.mirror(target)` returns `{left, right}` partners outermost first (an odd middle pairs
with itself), and `sf.side(index, count)` gives each member's half (-1, 0 for an odd middle,
1); `sf.side(index)` without a count keeps its alternating meaning, which agrees for pairs.
The room's positions (washes at X -900/-300/300/900, spots at -300/300, mm) already match
their names, so room.rig.pb and the goldens are unchanged.

2026-10-27 Group masters: each rig group has an inhibitive submaster, 0..1 and full by default,
kept in show.json as `settings.group_masters` (`{"wash": 0.5}`; an absent group is at full) and
sent as the `GROUP_MASTER` parameter with its `group` (protocol minor 2). Starfire multiplies the
group's cell intensity and attribute brightness by it right after the global master, so it
caps the rendered look, specials included, and never raises it; a cell in several capped
groups takes the product. Starfire
refuses an unknown group; Autolux sends only the levels of groups in the logged-in rig, and
only when one changes. It follows the same perceptual curve as the global master; it scales
intensity, and colour on fixtures without a Dimmer (a colour wheel holds its slot until 0, then
takes its dark slot); pan, tilt and gobo are untouched; inhibitive like grandMA3's Group Master.

2026-10-27 Mirror by symmetry: `sf.mirror(group)` pairs each member with the member nearest
its reflection about the stage centreline, the scene's X = 0 (MVR, DIN SPEC 15801: Matrix
"u1,u2,u3 / v1,v2,v3 / w1,w2,w3 / o1,o2,o3", "Right-handed, Z-Up, 1 Distance Unit equals 1
mm"; position is the o row). The reflection keeps Y and Z, so two rows that interleave in X
still pair within their row. A partner must be unique, mutual and within 300 mm of the
reflection; otherwise the group pairs by rank as before, with a warning. The 300 mm covers
plotting error (centimetres) and stays below fixture spacing (half a metre and more); missing
it only ever causes that fallback. Fixtures pair first; a mirrored pair's cells then pair in
reversed rank and a fixture on the axis mirrors its own cells by rank, without a warning. A
group wholly on one side of X = 0 (or a rig that never crosses it) mirrors about its own X
centre, also without a warning. Starfire derives the pairs once at rig load
(`starfire::spatial`) by searching each member's reflection within the tolerance window in X:
O(n log n) for spread members, O(n²) where many members share one X. It reads
`Fixture.matrix`, which the rig already carries; a cell takes its fixture's position since the
rig carries no GDTF geometry offsets, so the RigDefinition is unchanged. The room is symmetric
about X = 0, so its pairs equal the rank pairs and the goldens are unchanged.

2026-10-27 Zones from positions: Starfire places every cell from its fixture's MVR position
(MVR: "Right-handed, Z-Up, 1 Distance Unit equals 1 mm"). The plan axes follow GDTF (DIN SPEC
15800): "X – from left (-X) to right (+X), Y – from the outside of the monitor (-Y) to the
inside of the monitor (+Y)", read from the audience, since MVR itself names no audience side:
-X is house left, -Y downstage. The scene origin is the reference on both axes, like the
X = 0 centreline: `zone` is centre for |X| up to a third of the rig's largest |X|, else
house_left or house_right by sign (a rig that does not straddle X = 0 has no centreline and
falls back to thirds of its X span, centre third open); `depth` is upstage for Y >= 0, else
downstage, so a front-of-house truss does not move the split; `height` is Z. Lua:
`sf.zone(cell)` returns zone, depth, height; `sf.position(cell)` x, y, z; `sf.distance(cell,
x, y)` the plan distance; all in metres, converted only at the Lua boundary, and `cell` is a
cell name or an `sf.fixture` target. Login echoes each group's shared zone and depth (MIXED
when its members differ) and mean X in GroupCapability (protocol minor 2, still unreleased), so
a special role row may add `zone` or `depth` and play only on groups lying there. No new rig
field: `Fixture.matrix` already carries the position, so room.rig.pb is unchanged.
