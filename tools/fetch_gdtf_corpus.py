#!/usr/bin/env python3
"""Reproduce the GDTF/MVR test corpus under tests/data/corpus/.

Sources are real-world GDTF fixture files pulled from the official
mvrdevelopment/libMVRgdtf repo's unit-test fixtures (MVR SDK License v1.0,
permissive/zlib-style redistribution terms - see SOURCES.md). These are
genuine GDTF packages used by the reference MVR/GDTF SDK's own test suite,
including one real Robe BMFL Spot fixture.

Open Fixture Library (open-fixture-library.org) was evaluated as a source
but its "gdtf" plugin is import-only (plugins/gdtf/ has import.js, no
export.js as of 2026-10-26) - it cannot export .gdtf files, so it is not
used here. No redistributable sample .mvr files were located within the
time box; see SOURCES.md for the repos checked.

Run: python tools/fetch_gdtf_corpus.py
"""

from __future__ import annotations

import pathlib
import urllib.parse
import urllib.request

CORPUS_DIR = pathlib.Path(__file__).resolve().parent.parent / "tests" / "data" / "corpus"
GDTF_BASE = "https://raw.githubusercontent.com/mvrdevelopment/libMVRgdtf/master/unittest/files"

# (local filename, source filename on the branch above)
GDTF_FILES = [
    ("virtual_channel.gdtf", "virtual_channel.gdtf"),
    ("allWorking.gdtf", "allWorking.gdtf"),
    ("WrongDmxValue.gdtf", "WrongDmxValue.gdtf"),
    ("CrashWhileOpen.gdtf", "CrashWhileOpen.gdtf"),
    ("BugInDmxWithHex.gdtf", "BugInDmxWithHex.gdtf"),
    ("BugInChannelSetDmxJumps.gdtf", "BugInChannelSetDmxJumps.gdtf"),
    (
        "robe_bmfl_spot.gdtf",
        "Robe lighting s.r.o.@BMFL Spot@cannot assign 0 to frost function.gdtf",
    ),
]


def fetch(url: str, dest: pathlib.Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "autolux-corpus-fetch"})
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as out:
        out.write(resp.read())
    print(f"  {dest.name} <- {url}")


def main() -> None:
    gdtf_dir = CORPUS_DIR / "gdtf"
    gdtf_dir.mkdir(parents=True, exist_ok=True)
    print("Fetching GDTF corpus from mvrdevelopment/libMVRgdtf ...")
    for local_name, remote_name in GDTF_FILES:
        url = f"{GDTF_BASE}/{urllib.parse.quote(remote_name)}"
        fetch(url, gdtf_dir / local_name)

    mvr_dir = CORPUS_DIR / "mvr"
    mvr_dir.mkdir(parents=True, exist_ok=True)
    print(
        "No redistributable sample .mvr files found (see SOURCES.md) - "
        "mvr/ directory is intentionally empty pending a licensed source."
    )


if __name__ == "__main__":
    main()
