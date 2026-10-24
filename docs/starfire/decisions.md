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

## Prototype decisions

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

## Open questions and defaults

| ID | Question | Prototype default |
| --- | --- | --- |
| Q01 | When should a valid Lua edit activate? | Explicit reload; preserve musical position and never create a sequence restart command. Automatic next-bar reload remains optional. |
| Q02 | Should persistent manual colour overrides win over automatic accents? | Explicit priority; manual defaults above automatic on the same property. Unrelated intensity/strobe controls still combine. |
| Q03 | Named calibrated movement targets or direct pan/tilt first? | Direct normalized pan/tilt with calibrated bounds; API leaves room for named positions without needing a 3D editor. |
| Q04 | What are the exact CF805 and Mini Kinta IRC channel modes/calibrations? | Leave unverified hardware out of the audition patch; preserve addresses rather than guessing. |
| Q05 | Which manual controls belong in the first webapp tab? | Patch/groups and basic audition, master brightness and blackout; no browser code editor. |
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

## Validation and checkpoints

Record each slice's checks and the 15-minute reviews here as implementation lands.
Hardware audition is explicitly separate from deterministic offline validation.
