#!/usr/bin/env python3
"""Sanity-check the GDTF/MVR corpus: each file is a well-formed zip with the
expected root XML, and print the structural facts the parser/replay tests
care about (modes, channel counts, 16-bit channels, geometry references).

Run: python tools/check_corpus.py
"""

from __future__ import annotations

import pathlib
import sys
import zipfile
from xml.etree import ElementTree as ET

CORPUS_DIR = pathlib.Path(__file__).resolve().parent.parent / "tests" / "data" / "corpus"


def check_gdtf(path: pathlib.Path) -> None:
    with zipfile.ZipFile(path) as zf:
        if "description.xml" not in zf.namelist():
            print(f"{path.name}: FAIL - no description.xml (names: {zf.namelist()[:5]})")
            return
        root = ET.fromstring(zf.read("description.xml"))

    fixture = root.find("FixtureType")
    name = fixture.get("Name") if fixture is not None else "?"
    modes = root.findall(".//DMXModes/DMXMode")
    max_channels = 0
    has_16bit = False
    has_geom_ref = root.find(".//GeometryReference") is not None
    geometries = root.findall(".//Geometries/*")

    for mode in modes:
        channels = mode.findall(".//DMXChannels/DMXChannel")
        max_channels = max(max_channels, len(channels))
        for ch in channels:
            offset = ch.get("Offset", "")
            # Offset is a comma-separated list of byte addresses; >1 byte = 16-bit+.
            if offset and offset.lower() != "none" and len(offset.split(",")) >= 2:
                has_16bit = True

    print(
        f"{path.name}: '{name}' | modes={len(modes)} | max_channels={max_channels} "
        f"| geometries={len(geometries)} | GeometryReference={has_geom_ref} "
        f"| 16bit_channel={has_16bit}"
    )


def check_mvr(path: pathlib.Path) -> None:
    with zipfile.ZipFile(path) as zf:
        if "GeneralSceneDescription.xml" not in zf.namelist():
            print(f"{path.name}: FAIL - no GeneralSceneDescription.xml")
            return
        root = ET.fromstring(zf.read("GeneralSceneDescription.xml"))

    fixtures = root.findall(".//Fixture")
    layers = root.findall(".//Layer")
    print(f"{path.name}: fixtures={len(fixtures)} | layers={len(layers)}")


def main() -> int:
    gdtf_dir = CORPUS_DIR / "gdtf"
    mvr_dir = CORPUS_DIR / "mvr"
    ok = True

    print("--- GDTF ---")
    for path in sorted(gdtf_dir.glob("*.gdtf")):
        try:
            check_gdtf(path)
        except Exception as exc:  # noqa: BLE001 - report and keep going
            ok = False
            print(f"{path.name}: FAIL - {exc}")

    print("--- MVR ---")
    mvr_files = sorted(mvr_dir.glob("*.mvr"))
    if not mvr_files:
        print("(none - no redistributable sample .mvr files available, see SOURCES.md)")
    for path in mvr_files:
        try:
            check_mvr(path)
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"{path.name}: FAIL - {exc}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
