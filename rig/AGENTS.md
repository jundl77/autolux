# Rig files

- GDTF archives use the GDTF Share name `Manufacturer@Fixture Name@Revision.gdtf`, with the
  manufacturer and fixture written exactly as the manufacturer writes them (spaces allowed)
  and a required revision segment.
- The FixtureType `Manufacturer` and `Name` (and `LongName`, if present) inside the archive
  match the file name; the newest `Revision` `Text` is the revision segment.
- A FixtureType's `FixtureTypeID` GUID never changes across revisions or renames.
- `rig/library/` holds only real fixture types the venue owns. Synthetic profiles use the
  manufacturer `Generic` and live with the sample rig's test data
  (`autolux/tests/data/generic_rig/`), never in `rig/library/`.
- MVR files are named after the venue or show (`room.mvr`) and embed their GDTF archives
  under those same names.
- `*.rig.pb` is the exported Starfire rig: regenerate it with
  `autolux starfire export-rig`, never edit it by hand.
- `import-fixture` derives the file name from the archive's own fields and revision.
