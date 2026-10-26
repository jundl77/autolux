"""Discover which Lua autoloops each lighting intent chooses from, and what they declare."""

import re
from pathlib import Path

from autolux.lib.engine.autoloop_policy import AutoloopMeta, AutoloopMetaTable
from autolux.lib.engine.effect_definitions import PREFERRED_AUTOLOOPS, AutoloopTable

AUTOLOOP_DIRECTORY = Path(__file__).resolve().parents[2] / 'autoloops'
META_KEYS = ('energy', 'family', 'movement')
_META_LINE = re.compile(r'-- (\w+): (.*)')


class AutoloopMetaError(ValueError):
    pass


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
    return {loop for loops in discover_autoloops(directory).values() for loop in loops}


def parse_autoloop_meta(source: str) -> AutoloopMeta:
    """Read the `-- key: value` block right after the loop-length line."""
    values: dict[str, str] = {}
    for line in source.splitlines()[1:]:
        match = _META_LINE.fullmatch(line.rstrip())
        # The group lines below (`-- wash: ...`) share the shape, so the block
        # ends at the first other key; a misspelt key then reports as missing.
        if match is None or match[1] not in META_KEYS:
            break
        if match[1] in values:
            raise AutoloopMetaError(f'{match[1]} declared twice')
        values[match[1]] = match[2]
    missing = [key for key in META_KEYS if key not in values]
    if missing:
        raise AutoloopMetaError(
            f'missing {", ".join(missing)} in the header block after the loop-length line'
        )
    energy = values['energy']
    try:
        return AutoloopMeta(
            int(energy) if energy.isdigit() else energy, values['family'], values['movement']
        )
    except ValueError as error:
        raise AutoloopMetaError(str(error)) from None


def read_autoloop_meta(directory: Path = AUTOLOOP_DIRECTORY) -> AutoloopMetaTable:
    """Every discovered loop's metadata; one error names every loop that is wrong."""
    table, problems = {}, []
    for loop in sorted(required_autoloops(directory)):
        bank, name = loop
        path = Path(directory) / bank / f'{name}.lua'
        if not path.is_file():
            continue  # readiness names missing files on its own
        try:
            table[loop] = parse_autoloop_meta(path.read_text(encoding='utf-8'))
        except AutoloopMetaError as error:
            problems.append(f'{bank}/{name}: {error}')
    if problems:
        raise AutoloopMetaError('invalid autoloop metadata: ' + '; '.join(problems))
    return table
