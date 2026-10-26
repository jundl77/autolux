"""Discover which Lua autoloops each lighting intent rotates through."""

from pathlib import Path

from autolux.lib.engine.effect_definitions import (
    PREFERRED_AUTOLOOPS,
    STANDBY_AUTOLOOP,
    AutoloopTable,
)

AUTOLOOP_DIRECTORY = Path(__file__).resolve().parents[2] / 'autoloops'


def discover_autoloops(directory: Path = AUTOLOOP_DIRECTORY) -> AutoloopTable:
    table: AutoloopTable = {}
    for intent, preferred in PREFERRED_AUTOLOOPS.items():
        bank = intent.value
        names = sorted(path.stem for path in (Path(directory) / bank).glob('*.lua'))
        # The preferred loop stays listed even without its file, so readiness
        # reports it missing instead of the show quietly losing its opener.
        ordered = [preferred, *(name for name in names if name != preferred)]
        table[intent] = tuple((bank, name) for name in ordered)
    return table


def required_autoloops(directory: Path = AUTOLOOP_DIRECTORY) -> set[tuple[str, str]]:
    rotation = {loop for loops in discover_autoloops(directory).values() for loop in loops}
    return rotation | {STANDBY_AUTOLOOP}
