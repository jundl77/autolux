from collections.abc import Callable, Mapping
from pathlib import Path

from autolux.lib.engine.effect_definitions import (
    AUTOLOOP_META_VALUES,
    OVERLAY_BANKS,
    OVERLAY_META_VALUES,
    PREFERRED_AUTOLOOPS,
    STANDBY_AUTOLOOP,
    AutoloopMeta,
    AutoloopMetaTable,
    AutoloopTable,
    OverlayMeta,
    OverlayMetaTable,
)

AUTOLOOP_DIRECTORY = Path(__file__).resolve().parents[2] / 'show' / 'autoloops'


class AutoloopMetaError(ValueError):
    pass


def discover_autoloops(directory: Path = AUTOLOOP_DIRECTORY) -> AutoloopTable:
    table: AutoloopTable = {}
    for intent, preferred in PREFERRED_AUTOLOOPS.items():
        bank = intent.value
        names = sorted(path.stem for path in (Path(directory) / bank).glob('*.lua'))
        ordered = [preferred, *(name for name in names if name != preferred)]
        table[intent] = tuple((bank, name) for name in ordered)
    return table


def required_autoloops(directory: Path = AUTOLOOP_DIRECTORY) -> set[tuple[str, str]]:
    return rotation_autoloops(directory) | overlay_autoloops(directory) | {STANDBY_AUTOLOOP}


def rotation_autoloops(directory: Path = AUTOLOOP_DIRECTORY) -> set[tuple[str, str]]:
    return {loop for loops in discover_autoloops(directory).values() for loop in loops}


def overlay_autoloops(directory: Path = AUTOLOOP_DIRECTORY) -> set[tuple[str, str]]:
    return {
        (bank, path.stem)
        for bank in OVERLAY_BANKS
        for path in (Path(directory) / bank).glob('*.lua')
    }


def _header(source: str, allowed_values: Mapping[str, tuple[str, ...]]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in source.split('\n')[1:]:
        line = line.removesuffix('\r')
        key, separator, value = line.removeprefix('-- ').partition(': ')
        if not line.startswith('-- ') or not separator or key not in allowed_values:
            break
        allowed = allowed_values[key]
        if value not in allowed:
            raise AutoloopMetaError(f'{key} {value!r} is not one of {", ".join(allowed)}')
        if key in values:
            raise AutoloopMetaError(f'{key} declared twice')
        values[key] = value
    missing = [key for key in allowed_values if key not in values]
    if missing:
        raise AutoloopMetaError(
            f'missing {", ".join(missing)} in the header block after the first line'
        )
    return values


def parse_autoloop_meta(source: str) -> AutoloopMeta:
    values = _header(source, AUTOLOOP_META_VALUES)
    return AutoloopMeta(int(values['energy']), values['movement'], values['texture'])


def parse_overlay_meta(source: str) -> OverlayMeta:
    values = _header(source, OVERLAY_META_VALUES)
    return OverlayMeta(
        strobe=values['strobe'] == 'yes',
        intensity_htp=values['intensity'] == 'htp',
        movement=values['movement'],
        texture=values['texture'],
    )


def _read_meta(
    directory: Path, loops: set[tuple[str, str]], parse: Callable[[str], object], kind: str
) -> dict:
    table, problems = {}, []
    for loop in sorted(loops):
        bank, name = loop
        path = Path(directory) / bank / f'{name}.lua'
        if not path.is_file():
            continue
        try:
            table[loop] = parse(path.read_text(encoding='utf-8'))
        except AutoloopMetaError as error:
            problems.append(f'{bank}/{name}: {error}')
    if problems:
        raise AutoloopMetaError(f'invalid {kind} metadata: ' + '; '.join(problems))
    return table


def read_autoloop_meta(directory: Path = AUTOLOOP_DIRECTORY) -> AutoloopMetaTable:
    return _read_meta(directory, rotation_autoloops(directory), parse_autoloop_meta, 'autoloop')


def read_overlay_meta(directory: Path = AUTOLOOP_DIRECTORY) -> OverlayMetaTable:
    return _read_meta(directory, overlay_autoloops(directory), parse_overlay_meta, 'overlay')
