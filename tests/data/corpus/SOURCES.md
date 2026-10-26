# GDTF / MVR test corpus

Reproduce with `python tools/fetch_gdtf_corpus.py`. Re-run `python
tools/check_corpus.py` after any refresh to print the structural facts
below.

## GDTF files (`gdtf/`)

All seven files come from the official reference SDK's own unit-test
fixtures: `mvrdevelopment/libMVRgdtf`, path `unittest/files/`, `master`
branch, fetched 2026-10-26.

Licence: `LICENCE.md` in that repo is the "MVR SDK License, Version 1.0"
(Apache-2.0-derived grant from the GDTF Group - MA Lighting, Robe, Vectorworks
- plus a zlib-style redistribution clause: "Permission is granted to anyone
to use this software for any purpose, including commercial applications,
and to alter it and redistribute it freely", subject to not misrepresenting
origin and keeping the notice). Treated as permitting redistribution of
these test-fixture files with attribution, which this file provides.
https://github.com/mvrdevelopment/libMVRgdtf/blob/master/LICENCE.md

| File | Fixture name (from description.xml) | Modes | Max ch | Notes |
|---|---|---|---|---|
| `allWorking.gdtf` | Alpha Spot QWO 800 | 1 | 27 | moving head, 16-bit pan/tilt |
| `BugInDmxWithHex.gdtf` | Alpha Spot QWO 800 | 1 | 27 | same fixture, hex-DMX-value regression case |
| `BugInChannelSetDmxJumps.gdtf` | VivaCMY (Robe) | 2 | 24 | wash, CMY mixing, DMX-jump regression case |
| `CrashWhileOpen.gdtf` | Ayrton MagicBlade FX | 1 | 50 | multi-cell/pixel beam bar, parser-crash regression case |
| `WrongDmxValue.gdtf` | Robin 100 LEDBeam | 6 | 24 | multi-mode LED beam, bad-DMX-value regression case |
| `robe_bmfl_spot.gdtf` | BMFL Spot (Robe) | 2 | 31 | real production moving head, frost-function regression case |
| `virtual_channel.gdtf` | Basic Conventional | 1 | 3 | 2 geometries + GeometryReference, virtual-channel test case |

These are regression fixtures from the SDK's bug tracker (file names like
`CrashWhileOpen`, `WrongDmxValue` are the SDK's own), not clean manufacturer
exports - that is intentional: they are real GDTF content and exercise edge
cases (16-bit channels, multi-mode, GeometryReference, multi-geometry) that
a parser/replay test suite needs. `robe_bmfl_spot.gdtf` and the two
`Alpha Spot QWO 800` / `VivaCMY` files are genuine production fixtures.

Dropped from this corpus to stay well under budget / out of scope:
- `XMLBroken.gdtf` (1.5 KB, deliberately malformed XML - useful for error-path
  tests but not requested here; easy to add later from the same source).
- `MyModel*.glb` / `*.3ds` / `Fox.*` / stray `.png` screenshots in the same
  `unittest/files/` directory - these are standalone 3D-model/texture assets
  unrelated to GDTF parsing, several >10 MB each; excluded to keep the corpus
  small and on-topic.

Total corpus size: ~5.2 MB (all under `gdtf/`).

## MVR files (`mvr/`)

**None fetched.** Within the 25-minute time box:

- Open Fixture Library (open-fixture-library.org) does not export GDTF or
  MVR at all - its `plugins/gdtf/` only has `import.js`, no `export.js`
  (checked 2026-10-26 via
  https://github.com/OpenLightingProject/open-fixture-library/tree/master/plugins/gdtf).
  So step 1's premise (downloading `.gdtf` fixtures from OFL by URL) does not
  hold today; OFL's own data licence is MIT for the *site code*
  (`LICENSE`), and fixture *data* licensing is not stated on the About page
  checked.
- `mvrdevelopment/spec` has no top-level `LICENSE` file (GitHub's license
  API returns 404) and its `examples/` directory contains only markdown
  spec chapters, no sample `.mvr`/`.gdtf` binaries.
- `mvrdevelopment/libMVRgdtf` (licence above) has no `.mvr` files anywhere
  in its tree (confirmed via a full recursive tree search) - only the GDTF
  regression fixtures listed above.
- A quick web search for other public, clearly-licensed sample `.mvr` files
  (e.g. `open-stage/python-mvr`, MIT-licensed) did not turn up redistributable
  `.mvr` binaries within the time box.

Recommended next step (not done here): register for a free GDTF Share
(gdtf-share.com) account, which hosts manufacturer-submitted GDTF files and
some MVR show samples under its own terms - that requires login and is out
of scope for an unattended 25-minute fetch.
