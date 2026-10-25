# Starfire decision log

Status: prototype development on `starfire-prototype`. Every material decision
and unresolved question is recorded here for review. User decisions are binding;
prototype assumptions can change after an offline review and hardware audition.

## User decisions

| ID | Decision | Consequence |
| --- | --- | --- |
| U01 | Afterglow inspires the scripting API; SoundSwitch inspires the platform. | Fixture-aware, composable effects; fixtures, groups, banks and looping sequences. |
| U02 | Rust engine, Lua sequences. | Embedded Lua; no JVM, Wasm or Clojure compatibility requirement. |
| U03 | Autolux selects named sequences and transition policies. | Starfire executes commands; it does not classify music or select looks. |
| U04 | One autoloop corresponds to one Lua sequence. | Sequences repeat until changed. Selecting the active sequence is a no-op; no restart command. |
| U05 | Banks represent lighting categories. | Initial Atmospheric, Breakdown, Buildup and Drop banks; categories remain configuration. |
| U06 | Inputs match the useful timing/control data sent to SoundSwitch. | Beat/tempo/position/transport and controls; no PCM, energy bands or audio analysis in Starfire. |
| U07 | Radius owns timestamps and playback synchronization as implementation details. | Lua and the operator interface use musical time, never audio-frame watermarks. |
| U08 | Autolux controls accents and persistent overrides above the Lua baseline. | Strobe, brightness, speed and fixture colour can change independently of the sequence. |
| U09 | Named fixture groups make sequences portable between rigs. | Fixture setup maps roles such as wash and movers to actual hardware. |
| U10 | Keep script-failure handling simple. | No automatic bank failover or retry system. |
| U11 | Existing webapp gets fixture setup; keep one product entry point. | Root CLI coordinates components; no separate frontend. |
| U12 | Keep Art-Net output and the existing receiver network path initially. | No production Radius protocol replacement in this prototype. |
| U13 | Every Lua sequence has a deterministic beatgrid-to-DMX replay test. | Identical input, rig, controls and program produce identical DMX; live and replay share the renderer. |
| U14 | Work in an isolated worktree; commit meaningful slices to a branch. | Production master remains separate from prototype integration. |
| U15 | Delegate work, review it centrally and check progress every 15 minutes. | Three implementation workstreams; integration, decisions and final validation stay with the lead. |
| U16 | Calibrate the four Lua loops against captured output from the authored SoundSwitch loops. | Compare controlled beatgrids, levels, fixture encoding and timing; thematic similarity alone is insufficient. |
| U17 | Continue calibration while the user is away; speakers cannot be tested now. | Capture local Art-Net only; leave Radius and physical outputs stopped. Audible validation remains pending. |

## Prototype decisions

Latest scope: Starfire is the sole lighting backend on this branch. The user
approved removing SoundSwitch connectivity and requested the live contract in
`proto/starfire/starfire.proto`; 40 Hz remains the output cadence.

- P44: Use typed Google Protobuf commands and renderer events over the owned
  subprocess's stdin/stdout, framed with a four-byte big-endian payload length.
  Cap each payload at 64 KiB and validate protocol version 1. The root CLI owns
  launch, readiness and shutdown; no separately exposed control listener.
- P45: Apply accepted commands on the next 40 Hz render timestamp. The engine
  receives explicit time; monotonic pacing stays in the host. This preserves
  deterministic replay without assuming Python and Rust share a clock epoch.
  Art-Net ingress still samples DMX against Radius's audio watermark; exact
  target-watermark delivery remains a separate transport improvement.
- P46: Exchange renderer health and 10-second heartbeats; 30 seconds without
  controller input stops output. EOF, malformed input, queue overflow and fatal
  render errors attempt fixture-aware blackout and terminate the owned child.
  A musical Stop command stops the look without terminating the renderer.
- P47: Supply measured music energy separately from user master brightness.
  Normalize the analyser's smoothed RMS through a named -48 to -12 dBFS range,
  clamped to 0..1. This is measured loudness, not perceptual musical intensity.
  Missing energy in older replays defaults to 1; baseline programs may use a
  modest 0.65..1 brightness factor while maintaining their section character.
- P48: Remove the external channel-overlay protocol and mixer. Starfire owns
  all composition; Radius transports its complete DMX frames with audio.
- P49: Generate the Starfire Python descriptor with `protoc` in build output;
  load it through the official Protobuf runtime. Include it in built wheels so
  runtime installations need no compiler. Build/packaging hooks live in `tools/`,
  with the schema and codec under `proto/starfire`; no handwritten wire fields.
- P50: The existing show-delay queue dispatches beat and measured energy
  together. Replace channel-level note chasing with the selected Lua baseline.
  Drop entry requests `drop_arrival`: 8 Hz strobe, one second, priority 100.
  A committed class change within the same lighting bank keeps the current
  sequence and does not retrigger its entry accent. Persistent silence stops
  the renderer's look and discards queued commands that could reactivate it.
- P51: Reports name semantic sequences and accents, not transport channels.
  Remove the unmeasured constant beat-strength field; retain raw RMS and the
  original beat/section evidence. Compare those analysis invariants against the
  existing baseline without rewriting it. Validate changed lighting output with
  semantic command checks and deterministic DMX replay tests.
- P52: Keep the lighting beat ordinal monotonic for the full controller session;
  the analyser's per-song counter resets at silence and must not rewind Starfire.
  Audio level updates alongside detected beats and holds between them. A future
  continuous level stream also needs timestamped recording for replay parity.
- P53: Name the new measured control `normalized_rms_level`, with
  `SetNormalizedRmsLevel` and `rms_to_normalized_level`. Blind readers confused
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
  90-second show: minimum 10x real time for the six-fixture room and 2x for a
  64-fixture patch. These are conservative regression floors, not hard deadlines.
- P57: Use nonblocking Art-Net sends instead of a 100 ms socket timeout on the
  render thread. A send failure retains the existing fail-closed policy and
  attempts blackout. Do not add triple buffering or a backlog of completed DMX
  frames; consider a latest-frame handoff only if output separation is measured
  to be necessary. Radius's intentional synchronized playback buffer is separate.
- P58: Keep SoundSwitch reference control and comparison helpers in development
  tools or ignored validation artifacts. Production remains Starfire-only. Record
  original asset hashes, supplied controls and captured DMX; distinguish exact
  channel agreement from phase alignment and intentional profile differences.
- P59: Run the standalone dashboard synchronously from the CLI main thread.
  Its blocking server and mDNS registration must not occupy an asyncio event
  loop or an executor worker that prevents bounded Ctrl-C shutdown.
- P60: Calibrate against the saved Home patch using eight generic RGB pixels at
  addresses 97, 100, ... 118 for the CF805's verified 24-channel mode. Keep this
  reference patch separate until its scripts are ready. Mini Kinta channels
  40..43 remain dark; compare them explicitly rather than inventing a profile.
  Use Spot shutter value 7 and blue-wheel value 111, both inside the documented
  open/blue ranges. Prove existing replay changes contain only these equivalent
  channel encodings before updating their digests.
- P61: Record two complete 64-beat SoundSwitch cycles per look at 120 BPM and
  an independent 140 BPM run, with full master, normalized RMS 1 and no entry
  accent. Use one common temporal alignment per look; report active properties
  separately so inactive channels cannot dilute errors. Compare SoundSwitch's
  own repeat variability before choosing comparison tolerances. Reconstruct
  semantic authored curves; do not substitute raw DMX frame playback for Lua.
- P62: Preserve the renderer's rounded fixture encoding. SoundSwitch truncates
  some colour/intensity products; report the resulting one-byte quantization
  residual instead of adding a second encoding policy solely for comparison.
  Calibrated dimmer levels and movement coordinates remain semantic script values.
  Promote the verified strip patch with the reconstructed scripts, and retain
  named groups so programs remain independent of DMX addresses.
- P63: After reconstructing the programs, record a fresh 156 BPM reference run
  before evaluating it. Keep the formulas and alignment policy fixed for that
  final check; 120/140 BPM captures remain available for detailed diagnostics.
- P64: Readiness must execute each registered Lua program once against the
  selected fixture patch, with no network output, and reject script/group errors
  before starting Radius. Parsing a rig and listing filenames cannot detect an
  incompatible saved patch. This is a startup probe; replay tests cover the
  programs' later branches. Preserve custom saved patches; update this machine's
  saved default only after confirming it is the unchanged six-fixture default.

| ID | Decision and reason | Alternative / review point |
| --- | --- | --- |
| P01 | Install pinned Rust 1.99.1 MSVC, rustfmt and Clippy; use the existing VS 2022 C++ build tools. | Pin updates deliberately after validation. |
| P02 | Use embedded Lua 5.4 through `mlua`, with vendored Lua and explicit standard-library access. | Lua implementation can change without changing fixture semantics. |
| P03 | One Rust crate with separate timing, sequence, composition, fixture and output modules. | Avoid a framework, plugin graph or ECS before a concrete need. |
| P04 | The first working input is a versioned JSON replay/command stream, also usable from Autolux's recorded reports. | Freeze a production Protobuf contract under root `proto/` after API review; do not add an unused schema. |
| P05 | Render at 40 Hz by default; use supplied musical time rather than frame counts. | Test other cadences; no claim of sample-exact physical lighting. |
| P06 | Semantic intensity/colour/pan/tilt/strobe properties are composed before fixture encoding. | No raw-channel writing in normal sequences. |
| P07 | Overrides have explicit targets, identities, priorities and lifetimes; highest priority then latest command wins conflicting absolute properties. | Manual versus automatic priority defaults need user review (Q02). |
| P08 | Intensity multipliers compose with baseline intensity; a final master and blackout always win. | Absolute intensity remains available for persistent illumination. |
| P09 | Speed changes an integrated animation-beat clock; the musical beat clock stays unchanged. | Initial speed is global; per-group speed can follow a concrete use case. |
| P10 | A script fault stops lighting, releases strobe and outputs fixture-aware blackout; audio remains independent. | No automatic replacement. Failed reloads keep the previous program. |
| P11 | Start with verified Spot 110 and SePar profiles plus generic RGB/RGBW/dimmer fixtures. | Mini Kinta and CF805 need verified mode/profile data before real output (Q04). |
| P12 | All overnight validation is offline or UDP loopback. | Real fixture colour, movement and timing remain a later audition; never wake the room with test output. |
| P13 | Each sequence has a replay fixture and a checked full-frame DMX digest, plus semantic assertions. | Golden hashes alone are insufficient; assert expected light behaviour as well. |
| P14 | Reuse the existing Python environment explicitly without installing an editable worktree over production. | Tests verify imports resolve to the worktree; no duplicate dataset/model copies. |
| P15 | Embed a separate validated fixture-profile catalogue; rig files reference profile IDs. Preserve patch/group order. | Encoding performs no filesystem I/O. Verify nominal strobe-rate facts against manufacturer documentation. |
| P16 | Lua returns semantic effects using `sf.scene`, `sf.dimmer`, `sf.colour`, `sf.pan_tilt`, groups and oscillators. | Authors can calculate their own curves; helpers do not limit creative effects. |
| P17 | Bound each Lua VM to 16 MiB, 100,000 instructions per load/render, 128 KiB source and 512 effects per frame. | Named prototype limits; measure real sequences and adjust explicitly. No ambient clocks, I/O, imports or unseeded randomness. |
| P18 | Clock integration changes on beat/control events, not on rendered frames. | Replay cadence cannot change integrated animation speed. Beat-anchor corrections are distinct from speed changes. |
| P19 | Crossfade continuous semantic properties; switch discrete strobe to incoming value at fade completion. | Avoid invalid intermediate shutter functions. An interrupted fade captures its current baseline, bounding active VMs. |
| P20 | A fault retains the selected sequence identity in stopped state; duplicate selection cannot retry it. | Explicit selection of a different sequence recovers. Fault output clears overrides/strobe and preserves last valid movement. |
| P21 | Global animation speed and intensity multipliers accept 0..8; clamp composed intensity before the final master. | Named prototype bounds, separate from device-specific movement/strobe limits. |
| P22 | Implement strobe as deterministic intensity gating in the renderer; leave hardware shutter in its verified open range. Limit to 10 Hz at default 40 Hz rendering. | Manufacturer channel ranges do not establish calibrated Hz transfer curves for every fixture. Four samples/cycle improves representability; physical fidelity still needs audition. |
| P23 | Keep semantic requested state for diagnostics; encode a separate shutter-gated state. | Prevents diagnostics from confusing the requested strobe with a hardware rate byte. |
| P24 | Art-Net output is an explicit-destination ArtDmx adapter; support the user's existing broadcast path as an explicit compatibility option. | It is not a full Art-Net discovery/subscription node; default output is offline and tests use loopback. |
| P25 | Remove Lua `pcall`/`xpcall`, coroutines and debug access from authored sequences. | Scripts must not catch and indefinitely retry an instruction-budget failure. Catalogue limits: 256 sequences/4 MiB total; at most 512 resolved targets per render. |
| P26 | Determinism covers identical scripts, rig, events and render cadence. Bundled sequences derive animation from supplied time and also pass cadence-invariance checks. | Arbitrary Lua can retain closure state; document pure time-based authoring instead of recreating a VM every frame. |
| P27 | RGBW extracts the neutral RGB component; colour wheels use nearest normalized swatch and apply colour magnitude through the dimmer. | Physical colour balance remains subject to audition. Movement bounds and inversion are rig calibration. |
| P28 | Paced replay drops stale frames instead of bursting them after a stall. Completion, interruption and send failure attempt fixture-aware blackout at the last emitted positions. | UDP cannot guarantee delivery; receiver watchdog remains necessary. Use the standard `ctrlc` host handler, separate from the deterministic engine. |
| P29 | The first fixture tab edits and validates the patch only; audition and master controls remain CLI commands until a live renderer session exists. | Do not display inactive controls or create a second frontend. |
| P30 | Convert reports to musical source time: beat `t`, intent `song_t`, falling back to intent `t` minus the recorded look-ahead once. | Simulation can clamp delayed delivery at EOF. This is intended musical replay, not a faithful record of delivery lateness. Reports omit accents and downbeat ordinals; do not invent them. |
| P31 | Fixture edits validate through the Rust encoder before atomic replacement; revision checks reject stale browser edits. Changes apply on the next session. | Preserve group member ordering; no patch hot reload in the prototype. |
| P32 | Build checks run rustfmt, locked Clippy/tests and release compilation. The short production readiness check remains separate. | Do not add long suites to `--prod --check`. |
| P33 | Explicit network playback requires at least four frames per requested strobe cycle. | Lower-rate offline replays are sampling diagnostics; reject aliased physical strobe output. |
| P34 | Report conversion infers beat ordinals from detections, skips zero-tempo warm-up and holds the last positive tempo afterward. Silence and EOF produce Stop. | Reports do not preserve true bar origin; exact beat/downbeat phase requires a richer live capture contract. |
| P35 | Root CLI requests blackout using an owned stop-file signal before bounded child shutdown. Native Ctrl-C remains available. | Windows headless children cannot reliably receive console signals; force termination remains the final timeout fallback. |
| P36 | Bound rig input to 1 MiB, fixture/group counts to 512 and identifiers to 128 bytes. Bound latched diagnostics to 1 KiB and replay diagnostic storage to 8 MiB. | Lua memory limits alone do not bound host copies of errors or rig identifiers; adversarial resource regressions cover these boundaries. |
| P37 | Replay defaults to the dashboard's saved rig when present, otherwise the committed room rig; explicit paths win. | A custom fixture-store path requires the matching explicit replay rig. |
| P38 | Beat corrections update authoritative musical position; animation phase integrates elapsed tempo/speed continuously and does not jump with corrections. | Speed-scaled phase corrections could make animation negative and visibly restart it. Authors can use beat_position for strict beat phase; a phase-lock/slew policy is a future review question. |
| P39 | Blind naming review covers output stages, override lifetimes and the two sequence clocks. Rename ambiguous public frame fields and make the Lua speed-clock origin/units explicit. | See the naming review for exact reader predictions and retained contextual ambiguities; no exhaustive identifier pass. |
| P40 | Buildup and Drop intensity oscillators follow absolute musical beat position, including phase corrections and selection between beats. Movement and colour use the continuous speed-scaled sequence clock. | A free-running animation clock can drift from detected beats; a sequence-relative origin can offset every pulse after a mid-beat selection. Speed controls those animations without desynchronizing intensity from musical beats. |
| P41 | Fixture rows retain their original identity during edits so renaming preserves ordered group membership. | Group order drives spatial phase; treating a renamed fixture as a new member silently changes choreography. |
| P42 | Reject capture/summary paths that collide with replay input, rig input or each other before writing. | Explicit output paths must not accidentally destroy the audition inputs or replace a binary capture with its summary. |
| P43 | Exclude Lua pointer/identity formatting from the authoring environment. | `tostring` on tables/functions and pointer-capable `string.format` expose allocator addresses, an ambient source of nondeterministic effects. Sequence math and ordinary string concatenation do not need them. |

## Open questions and defaults

| ID | Question | Prototype default |
| --- | --- | --- |
| Q01 | When should a valid Lua edit activate? | Start a fresh replay after editing in the prototype. Live reload should preserve musical position; explicit versus next-bar activation remains open. No restart command. |
| Q02 | Should persistent manual colour overrides win over automatic accents? | Explicit priority; manual defaults above automatic on the same property. Unrelated intensity/strobe controls still combine. |
| Q03 | Named calibrated movement targets or direct pan/tilt first? | Direct normalized pan/tilt with calibrated bounds; API leaves room for named positions without needing a 3D editor. |
| Q04 | What are the exact CF805 and Mini Kinta IRC channel modes/calibrations? | Leave unverified hardware out of the audition patch; preserve addresses rather than guessing. |
| Q05 | Which manual controls belong in the first webapp tab? | Patch/groups now; live audition, master and blackout follow the live-session integration. No browser code editor. |
| Q06 | Where does Art-Net sit when Radius preserves exact render targets? | Preserve existing local Art-Net ingress in the prototype; document measured scheduling limits. Clock mapping remains Radius implementation work. |
| Q07 | Are multiple universes needed immediately? | Encoder/output support universe identity; current Radius deployment remains its existing configured universe. |

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
the continuing baseline; it never restarts a sequence. Held overrides survive
sequence changes until cleared or stopped. Rust produces one composed DMX frame.

Speed is a supplied animation control, not a transformation of already encoded
DMX. Integrate animation beats continuously through speed changes while keeping
the musical beatgrid unchanged. Per-group speed remains a later extension.

The fixtures tab maps named roles to concrete fixtures. Sequence identity is a
registered bank/name pair, never an arbitrary caller-provided file path.

## Validation and checkpoints

Record each slice's checks and the 15-minute reviews here as implementation lands.
Hardware audition is explicitly separate from deterministic offline validation.

- 2026-10-25 09:12 local: migration checkpoint. All three implementation slices
  and cross-component reviews returned. Native Radius build and four test groups
  pass; Rust covers 88 tests and a real 30-second heartbeat expiry with blackout.
  Review fixed per-song beat resets and full-queue shutdown. Full Python tests
  are running, with analysis baselines unchanged. Naming review found the new
  measured-level API should distinguish RMS measurement from perceived intensity.
- 2026-10-25 09:27 local: complete product build and 260 focused final checks pass.
  The 90-second live Protobuf/Art-Net loopback delivered all 3,602 packets without
  sequence gaps, exercised all four banks and shut down with blackout preserving
  movement. Production readiness passed in under three seconds. Full Python run
  found the expected changed-report hash and an incomplete build test; the latter
  is fixed. Strict old/new ten-track analysis comparison continues separately.
- 2026-10-25 09:42 local: final review. All ten tracks retain identical analysis;
  only verified output-report hashes changed, and the failed benchmark test now
  passes. Twenty native replays reproduce each track's DMX digest exactly. All
  91 Rust tests pass, including performance floors: release measurements are
  530.4x for the room and 82.2x for 64 fixtures. Nonblocking output and production
  universe validation are covered. Final slices published; the timer continues
  for the subsequently requested hardware run and reference calibration.
- 2026-10-25 09:57 local: hardware run completed with user-confirmed lighting.
  Audio transport had no packet loss, buffer overflows or buffer underruns; the
  speaker was off, so audible output remains unconfirmed. The run exposed an
  mDNS/dashboard lifecycle defect, now under regression review. Reference-loop
  calibration takes priority while the user is away; local and remote Radius
  processes are absent, and SoundSwitch output is captured on localhost only.
- 2026-10-25 10:12 local: all three agents reviewed. Dashboard lifecycle fix is
  published with 53 passing CLI checks. Fixture encoding is published with 92
  passing Rust checks and proof that only equivalent blue/shutter bytes changed.
  Both 120 and 140 BPM reference captures completed, covering two full cycles of
  every look. Native palette/keyframe extraction and independent curve comparison
  continue. No Radius, renderer or physical-output process is running.
- 2026-10-25 10:27 local: native files and independent captures establish compact
  colour, intensity and movement formulas. Lua reconstruction and 14-fixture room
  patch are under regression review. Comparison budgets are frozen from reference
  cadence before candidate evaluation; direct captured-value anchors will check
  finer waveform accuracy. Review found startup did not execute scripts against
  the saved rig; that readiness defect is being fixed before final publication.

- 2026-10-25 01:47 local: first team checkpoint. Fixture/output, bounded Lua and
  engine suites pass independently. Lead reviewed public boundaries and found
  replay output needed safe shutdown and stale-frame skipping. Agents reassigned
  to existing dashboard setup, Autolux report/CLI integration and build checks.
  Real fixtures remain idle. Background 15-minute checkpoint timer is active.
- 2026-10-25 02:02 local: second checkpoint. All delegated implementation and
  independent reviews returned. Native replay, output lifecycle, bounded Lua,
  patch persistence and report timing checks pass. Manual Edge review found and
  fixed group-order and dropdown/reload issues. Naming review is bounded to three
  API concepts. Full repository validation is compiling native dependencies;
  Python/GPU workloads remain serialized. Four core/build commits are recorded.
- 2026-10-25 02:17 local: third checkpoint. Full native build, four Radius test
  groups and 76 Rust tests pass. The 90-second root-CLI loopback audition delivered
  all 3,602 packets in order with exact offline payload parity and final blackout;
  maximum observed interval was 25.93 ms. Python tests are running without failures;
  backtest/simulation follow serially. All delegated work is reviewed and committed.
- 2026-10-25 02:32 local: fourth checkpoint. Python completed with 2,343 passes
  and one missing-warm-cache skip; that case will be rerun after cache generation.
  The final Lua identity-reflection fix is committed; 77 Rust tests, strict
  Clippy and the release rebuild pass. Final executable capture remains identical
  to the paced reference. Backtest is at 5/10 tracks, generating missing caches
  in this worktree. No baseline updates or additional feature work are planned.
- 2026-10-25 02:41 local: final checkpoint. Full validation passed; all ten
  backtest reports match the committed baseline and standalone simulation passes.
  The initially skipped warm-cache case now passes, completing all 2,344 Python
  cases. Final Rust checks cover 77 tests and the release capture matches the
  paced reference. See [validation](validation.md) for evidence and limits.
  Publishing the review branch and stopping the owned checkpoint timer.
