# Rig file: MVR 1.6 with embedded GDTF

`rigs/room.mvr` is an MVR 1.6 (DIN SPEC 15801) archive: `GeneralSceneDescription.xml`
plus every `.gdtf` (DIN SPEC 15800, `description.xml` inside) its fixtures reference, all
at the archive root, STORE or DEFLATE. GDTF files are named
`<Manufacturer>@<FixtureType>.gdtf`. `rigs/profiles.json` is only the fixture *library*:
Config embeds a library GDTF into the MVR when a fixture first uses it.
`tools/rig_json_to_mvr.py` converts legacy JSON rigs.

## Contract

Autolux reads and validates the MVR (`autolux/lighting/mvr*.py`) and fills the existing
`RigDefinition` proto: one `Fixture` per patched MVR Fixture (name, `GDTFSpec#GDTFMode`
profile, universe/address, scene-space `matrix`, DMXInvertPan/Tilt and Autolux movement
limits), classes as groups, embedded GDTF XML as profiles. Starfire's own reader
(`starfire/src/mvr.rs`) builds the same `RigDefinition` for `check`, `rig` and `replay`,
so both paths share one validator (`FixtureRig::from_wire`). Proto stays the contract;
Login does not carry archive bytes.

Patched = a `Fixture` anywhere under a Layer with `GDTFSpec` and an `Address`; its Matrix
is composed through every parent (Layer, GroupObject, Truss, …). Starfire renders one
Address on break 0 per fixture; other objects and unpatched fixtures are planning data.
Class member order is MVR document order. Multipatch fixtures render as separate patches.

## Orientation

`Matrix` rows are the local X, Y, Z axes in parent space, then the position in mm
(Z up, right-handed). The rotation is read as `R = Rz(yaw) · Rx(pitch) · Ry(roll)`:

- pan home offset = scene-space **yaw**, canonical in (-180°, 180°] (+180, never -180,
  so one orientation always gives the same DMX on 540° heads);
- tilt home offset = **pitch**; roll is shown and stored but not rendered;
- identity = the GDTF default orientation = pan/tilt offset 0. The room spots are
  `{-1,0,0}{0,-1,0}{0,0,1}{…}`, i.e. yaw 180, reproducing the previous 180° offset;
  the four golden DMX digests are unchanged.

Sheared, mirrored or degenerate matrices are rejected. The Config plan is a top view
(+Y up); each arrow is the object's local Y axis.

## Validation on save

Config refuses a save, naming the node and rule, unless the archive passes
`mvr_validation.validate` (every MVR table: allowed children and attributes, mandatory
counts, value types — Integer, Float, Bool, UUID (non-nil), FileName, Matrix, CIE xyY,
Vector, IPv4/IPv6, enums, DMX address `1..16777216` or `Universe.Address` — UUID
uniqueness, references resolving to the right node type (Classing, Position, Focus,
symdef, linkedDef, toObject, multipatch), referenced files present, GDTFMode mandatory with
GDTFSpec and present in that GDTF, unique Address breaks, break `n` ↔ GDTF `DMXBreak n+1`,
multipatch children without FixtureID/CustomId), then Autolux's patch rules (unique
names, overlaps, footprint), then `starfire rig`.

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

Comments, whitespace and attribute order are not preserved; unchanged archives re-save
byte-identically. Child order is not validated (the spec examples vary); new children are
inserted in table order. Network attributes follow Table 52 (`ipv4`, `dhcp`, …), not the
differently cased example.

## GDTF support

Every GDTF node and attribute is shown per fixture, read-only, except:
DMXMode `Name`/`Description`; ChannelFunction `Name`, `DMXFrom`, `Default`,
`PhysicalFrom`, `PhysicalTo`, `RealFade`, `RealAcceleration`, `Min`, `Max`, `CustomName`;
ChannelSet `Name`, `DMXFrom`, `PhysicalFrom`, `PhysicalTo`. Edits are checked for Name
characters (Annex C), DMXValue (`n/b[s]`), finite floats, unique mode names and an
unchanged structure; a changed GDTF gains a `Revision` and is re-zipped with its other
entries, an unchanged one is kept byte for byte. Full GDTF XSD validation is not run;
Starfire's GDTF compiler rejects modes it cannot render (multiple breaks, geometry
references, relations).
