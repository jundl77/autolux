"""Discover which Lua autoloops each lighting intent chooses from, and what they declare."""

from pathlib import Path

from autolux.lib.engine.effect_definitions import (
    AUTOLOOP_META_VALUES,
    PREFERRED_AUTOLOOPS,
    STANDBY_AUTOLOOP,
    AutoloopMeta,
    AutoloopMetaTable,
    AutoloopTable,
)

AUTOLOOP_DIRECTORY = Path(__file__).resolve().parents[2] / 'autoloops'


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
    return rotation_autoloops(directory) | {STANDBY_AUTOLOOP}


def rotation_autoloops(directory: Path = AUTOLOOP_DIRECTORY) -> set[tuple[str, str]]:
    return {loop for loops in discover_autoloops(directory).values() for loop in loops}


def parse_autoloop_meta(source: str) -> AutoloopMeta:
    """Read the `-- key: value` block right after the loop-length line.

    Mirrors `metadata` in `starfire/tests/autoloop_replay.rs` rule for rule:
    values match the vocabulary exactly, with no padding or leading zeros.
    """
    values: dict[str, str] = {}
    # Rust's `lines()`: split on \n, drop one trailing \r.
    for line in source.split('\n')[1:]:
        line = line.removesuffix('\r')
        key, separator, value = line.removeprefix('-- ').partition(': ')
        # The group lines below (`-- wash: ...`) share the shape, so the block
        # ends at the first other key; a misspelt key then reports as missing.
        if not line.startswith('-- ') or not separator or key not in AUTOLOOP_META_VALUES:
            break
        allowed = AUTOLOOP_META_VALUES[key]
        if value not in allowed:
            raise AutoloopMetaError(f'{key} {value!r} is not one of {", ".join(allowed)}')
        if key in values:
            raise AutoloopMetaError(f'{key} declared twice')
        values[key] = value
    missing = [key for key in AUTOLOOP_META_VALUES if key not in values]
    if missing:
        raise AutoloopMetaError(
            f'missing {", ".join(missing)} in the header block after the loop-length line'
        )
    return AutoloopMeta(int(values['energy']), values['family'], values['movement'])


def read_autoloop_meta(directory: Path = AUTOLOOP_DIRECTORY) -> AutoloopMetaTable:
    """Every discovered loop's metadata; one error names every loop that is wrong."""
    table, problems = {}, []
    # Standby is never chosen by the policy, so it carries no selection metadata.
    for loop in sorted(rotation_autoloops(directory)):
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
