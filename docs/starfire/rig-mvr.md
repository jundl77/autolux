# Rig file: MVR 1.6 with embedded GDTF

`rigs/room.mvr` is an MVR 1.6 (DIN SPEC 15801) archive: `GeneralSceneDescription.xml`
plus every `.gdtf` (DIN SPEC 15800, `description.xml` inside) its fixtures reference, all
at the archive root, STORE or DEFLATE. GDTF files are named in GDTF Share
form, `Manufacturer@Fixture Name@Revision.gdtf` (see [rigs/AGENTS.md](../../rigs/AGENTS.md)).
`rigs/library/` holds the venue's fixture *library* as GDTF 1.2 archives under the same
names: Config embeds a library archive into the MVR byte for byte
when a fixture first uses it, so a rig rebuilt from the library exports the same
`RigDefinition`.

## Contract

Autolux is the only MVR reader (`autolux/lighting/mvr*.py`). It validates the archive and
fills the `RigDefinition` proto: one `Fixture` per patched MVR Fixture (name,
`GDTFSpec#GDTFMode` profile, universe/address, scene-space `matrix`, DMXInvertPan/Tilt
and Autolux movement limits), classes as groups, embedded GDTF XML as profiles. Login
carries it; the offline `starfire check`, `rig` and `replay` commands read the same
message serialized to a file (`autolux starfire export-rig rig.mvr rig.rig.pb`; the
Autolux CLI exports to a temporary file automatically). `FixtureRig::from_wire` is the
single native validator. `rigs/room.rig.pb` and the synthetic sample rig
`autolux/tests/data/generic_rig/generic.rig.pb` are the exports the Rust tests and goldens use; `cli/tests/test_starfire_assets.py` checks they equal
the Login rig of their MVR.

Patched = a `Fixture` anywhere under a Layer with `GDTFSpec` and an `Address`; its Matrix
is composed through every parent (Layer, GroupObject, Truss, …). Starfire renders one
Address on break 0 per fixture; other objects, unpatched fixtures and unreferenced GDTF
files are planning data. Class member order is MVR document order. Multipatch fixtures
render as separate patches.

## Orientation

`Matrix` rows are the local X, Y, Z axes in parent space, then the position in mm
(Z up, right-handed). The rotation is read as `R = Rz(yaw) · Rx(pitch) · Ry(roll)`:

- pan home offset = scene-space **yaw**, canonical in (-180°, 180°] (+180, never -180,
  so one orientation always gives the same DMX on 540° heads);
- tilt home offset = **pitch**. This is an approximation: it is exact only at pan home,
  because a tilted base couples pan into tilt. |pitch| > 60° is refused;
- a local Z axis pointing down **reverses** pan and tilt (as DMXInvertPan/Tilt would;
  setting those flags too cancels it again); roll otherwise does not render;
- identity = the GDTF default (hanging) orientation = offset 0, not reversed.

Hanging, turned around: `Rz(180)` = `{-1,0,0}{0,-1,0}{0,0,1}{x,y,z}` → yaw 180, not
reversed. This is how the room spots are entered. Standing on the floor: `Rx(180)` =
`{1,0,0}{0,-1,0}{0,0,-1}{x,y,z}` (Config: pitch 0, roll 180, yaw 180 as decomposed) →
yaw 180 and reversed. The inspector shows the derived home under each Matrix.

Sheared, mirrored or degenerate matrices are rejected. The Config plan is a top view
(+Y up); each arrow is the object's local Y axis.

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
