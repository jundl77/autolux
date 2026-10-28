# Rig file: MVR 1.6 with embedded GDTF

`rig/room.mvr` is an MVR 1.6 (DIN SPEC 15801) archive: `GeneralSceneDescription.xml`
plus every `.gdtf` (DIN SPEC 15800, `description.xml` inside) its fixtures reference, all
at the archive root, STORE or DEFLATE. GDTF files are named in GDTF Share
form, `Manufacturer@Fixture Name@Revision.gdtf` (see [rig/AGENTS.md](../../rig/AGENTS.md)).
`rig/library/` holds the venue's fixture *library* as GDTF 1.2 archives under the same
names: Config embeds a library archive into the MVR byte for byte
when a fixture first uses it, so a rig rebuilt from the library exports the same
`RigDefinition`.

## Contract

Autolux is the only MVR reader (`autolux/lighting/mvr*.py`). It validates the archive and
fills the `RigDefinition` proto: one `Fixture` per patched MVR Fixture (name,
`GDTFSpec#GDTFMode` profile, universe/address, scene-space `matrix`, DMXInvertPan/Tilt
and Autolux movement limits), classes as groups, each embedded GDTF's mode compiled to a
typed `DmxMode` as profiles. Login
carries it; the offline `starfire check`, `rig` and `replay` commands read the same
message serialized to a file (`autolux starfire export-rig rig.mvr rig.rig.pb`; the
Autolux CLI exports to a temporary file automatically). `FixtureRig::from_wire` is the
single native validator. The synthetic sample rigs `autolux/tests/data/generic_rig/stage.rig.pb`
and `generic.rig.pb` are the exports the Rust tests and goldens use; `cli/tests/test_starfire_assets.py`
checks they and `rig/room.rig.pb` equal the Login rig of their MVR.

Identity is the MVR Fixture `uuid`; `name` is a label; `FixtureID`/`FixtureIDNumeric` are the
console patch numbers and identify nothing. The uuid is `Fixture.fixture_uuid`, every cell id
derives from it (`uuid`, or `uuid/geometry` on a multi-cell fixture), group members are the
uuids the `Classing` attribute places in each Class, and highlight, override and test targets
carry it. `Fixture.name` reaches Starfire for logs and for autoloops, which name fixtures by
label (`sf.fixture("kinta")`, resolved to the uuid in Starfire), so Starfire refuses a rig that
repeats a uuid or a name. Renaming a fixture, or swapping two names, moves nothing else. Config
gives each fixture it adds a fresh random (version 4) uuid.

Patched = a `Fixture` anywhere under a Layer with `GDTFSpec` and an `Address`; its Matrix
is composed through every parent (Layer, GroupObject, Truss, …). Starfire renders one
Address on break 0 per fixture; other objects, unpatched fixtures and unreferenced GDTF
files are planning data. Class member order is MVR document order. Multipatch fixtures
render as separate patches.

## Orientation

`Matrix` rows are the local X, Y, Z axes in parent space, then the position in mm
(Z up, right-handed). The rotation is read as `R = Rz(yaw) · Rx(pitch) · Ry(roll)`:

- **yaw** is the direction the fixture's display faces on the plan (the local +Y axis,
  anticlockwise from scene +Y); at physical pan 0 the beam leaves that way. Pan: physical angle
  0 is the fixture's modelled orientation (GDTF); a profile whose range is declared 0..N claims
  DMX 0 is that orientation, correct profiles are declared symmetric around the home. A logical
  pan is the stage heading L (clockwise from +Y) measured from physical 0, and a hanging head
  reaches it at physical L + yaw; the pan home offset is canonical in
  (-180°, 180°] (+180, never -180, so one orientation always gives the same DMX on 540°
  heads);
- tilt home offset = **pitch**. This is an approximation: it is exact only at pan home,
  because a tilted base couples pan into tilt. |pitch| > 60° is refused;
- a local Z axis pointing down **reverses** tilt (as DMXInvertTilt would; setting it too
  cancels it again) and mirrors pan over its whole travel with the yaw turned the other way,
  so a logical pan lands on the same stage heading as on a hanging head at any yaw; roll
  otherwise does not render;
- a standing head pans the other way round, which Starfire accounts for: it reaches heading L
  at physical 180 - L - yaw (kept in range by whole turns), because its reversed tilt throws the
  beam a half turn;
- identity = the GDTF default (hanging) orientation, display to upstage: offset 0, not
  reversed.

Hanging, turned around: `Rz(180)` = `{-1,0,0}{0,-1,0}{0,0,1}{x,y,z}` → yaw 180 (display to the
audience), not reversed. Standing on the floor: `Rx(180)` =
`{1,0,0}{0,-1,0}{0,0,-1}{x,y,z}` → yaw 180 and reversed; Config shows it as Standing,
yaw 180, since its Mount toggle negates the local X and Z and so keeps the facing. The inspector shows the derived home under each Matrix.

Sheared, mirrored or degenerate matrices are rejected.

## Plan and stage directions

MVR fixes the frame but names no audience side and gives its origin no meaning; Autolux reads
the plan as GDTF draws a device, with the audience as the viewer (-X house left, -Y downstage;
the standard texts are quoted at `stage_areas` in `cli/starfire_assets.py`). Each fixture's
`StageArea` is its cell of the nine-area stage grid over the rig's bounding box: X and Y cut
into equal thirds, a fixture on a cut in the centre third, an axis without extent all centre.
Stage areas use the rig's bounding box, while `sf.mirror` keeps the scene's X = 0 as
its axis, with the per-group fallback: two separate concepts.
The Config rig map is a standard plot from the same draft: plan view, audience at the bottom,
the dashed nine-area grid over the fixtures' bounding box, the stage edge at the most downstage
object, a 1-2-5 metre scale
bar, one ring per fixture coloured by group (golden-angle hues past the configured six) and
dashed for an accent-layer group, an arrow along the local Y axis (yaw, the display), and a legend. Names
label the marks up to 48 fixtures; past that marks shrink with density and names move to the
hover/tap title, which keeps a 500-fixture map under 200 KB. It redraws whenever the draft
changes, and reads positions only; the MVR changes only through the Config editor's writer.

## Validation on save

`autolux/lighting/mvr_schema.py` holds the MVR tables as one declarative text table: one
line per node with its attributes, children, value types, cardinality (`!` required,
`?` 0..1, `*` 0..n, none = exactly one) and reference targets (`>Class`). The validator
(`mvr_validation.py`), the draft templates and the Config inspector all read it.

Config refuses a save, naming the node and rule, unless the archive passes
`mvr_validation.validate`: allowed children and attributes, mandatory counts, value types
(Integer, Float, Bool, UUID (non-nil), FileName, Matrix, CIE xyY, Vector, IPv4/IPv6,
enums, DMX address `1..16777216` or `Universe.Address`), UUID uniqueness, references
resolving to the right node type (Classing, Position, Focus, symdef, linkedDef, toObject,
multipatch), referenced files present, GDTFMode mandatory with GDTFSpec and present in
that GDTF, unique Address breaks, break `n` ↔ GDTF `DMXBreak n+1`, multipatch children
without FixtureID/CustomId. Autolux's render rules follow (unique names, one break-0
Address, pitch limit, orthonormal matrices, footprint, overlaps), then `starfire rig` on
the exported `RigDefinition` (GDTF mode support).

## Element support

| MVR element | Read and validated | Editable in Config | Rendered |
| --- | --- | --- | --- |
| GeneralSceneDescription, Scene, Layers, Layer (+Matrix), ChildList | yes | yes (provider/version/verMinor are rewritten on save) | Layer Matrix composed |
| UserData, Data (other providers) | yes (attributes) | add/remove; content preserved verbatim | no |
| Data provider="Autolux" › Movement (fixture, axis, minimum, maximum) | yes | yes | pan/tilt limits — **Autolux extension** |
| AUXData › Class | yes | yes | groups |
| AUXData › Symdef (ChildList › Geometry3D/Symbol), Position, MappingDefinition (SizeX, SizeY, Source, ScaleHandeling) | yes | yes | no |
| Fixture: uuid, name, multipatch, Matrix, Classing, GDTFSpec, GDTFMode, Addresses/Address(break) | yes | yes | yes |
| Fixture: DMXInvertPan, DMXInvertTilt | yes | yes | yes |
| Fixture: Focus, CastShadow, Position, Function, FixtureID, FixtureIDNumeric, UnitNumber, ChildPosition, Color, CustomIdType, CustomId, Gobo(rotation) | yes | yes | no |
| Fixture: Protocols/Protocol, Alignments/Alignment, CustomCommands/CustomCommand, Overwrites/Overwrite, Connections/Connection, Mappings/Mapping (ux…rz), Addresses/Network, ChildList | yes | yes | no (ChildPosition offsets are not composed) |
| GroupObject, FocusPoint, Truss, Support, SceneObject, VideoScreen (Sources/Source), Projector (Projections/Projection), Geometries, Geometry3D, Symbol | yes | yes | Matrix composed for children |
| Any element or attribute outside the tables | read | shown, removable | save refused |

Layer › ChildList is 0..1 in the published 1.6 tables (tag `1.6-mvr`); Autolux accepts
that but always writes one. The third-party test sample is synthetic
(`autolux/tests/data/mvr/sample_scene.xml`): the spec repository ships GDTF snippets but no
exported `.mvr`. Comments, whitespace and attribute order are not preserved; unchanged
archives re-save byte-identically. Child order is not validated (the spec examples vary);
new children are inserted in table order. Network attributes follow Table 52 (`ipv4`,
`dhcp`, …), not the differently cased example.

## GDTF support

The library types follow DIN SPEC 15800 Annex B: standard features (Dimmer.Dimmer,
Position.PanTilt, Color.RGB/Color.Color, Beam.Beam, Gobo.Gobo, Control.Control only for
control channels), ColorAdd_* in ColorComponent, Pan/Tilt on `Axis` geometries (Base ›
Yoke › Head › Beam), light on a `Beam` geometry, upper-case FixtureTypeID, primitive
`Models` and an sRGB `PhysicalDescriptions`. Channels Autolux holds at a default carry their
real attributes (Shutter1, Gobo1, PositionMSpeed, Function, PositionEffect(Rate), ColorAdd_UV,
ColorMacro1(Rate)).

Every GDTF node and attribute is shown per fixture, read-only, except:
DMXMode `Name`/`Description`; ChannelFunction `Name`, `DMXFrom`, `Default`,
`PhysicalFrom`, `PhysicalTo`, `RealFade`, `RealAcceleration`, `Min`, `Max`, `CustomName`;
ChannelSet `Name`, `DMXFrom`, `PhysicalFrom`, `PhysicalTo` (`GDTF_EDITABLE`). Edits are
checked for Name characters (Annex C), DMXValue (`n/b[s]`), finite floats, unique mode
names and an unchanged structure; a changed GDTF gains a `Revision` and is re-zipped with
its other entries, an unchanged one is kept byte for byte. Full GDTF XSD validation is not
run; Starfire's GDTF compiler rejects modes it cannot render (multiple breaks, geometry
references, relations).
