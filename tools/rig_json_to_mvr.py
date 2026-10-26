"""Convert a pre-MVR rig.json fixture patch into an MVR archive."""

import argparse
import json
from pathlib import Path
from xml.etree import ElementTree

from autolux.lighting import mvr
from autolux.lighting.fixture_setup import require_renderable
from autolux.lighting.gdtf import fixture_library

ROOT = Path(__file__).resolve().parents[1]
ROW_SPACING_MM = 1000.0
FIXTURE_SPACING_MM = 600.0
FIELDS = {
    'id',
    'profile',
    'universe',
    'address',
    'pan',
    'tilt',
    'pan_offset_degrees',
    'tilt_offset_degrees',
}


def convert(rig: dict, library: dict, rig_name: str) -> bytes:
    if rig.get('schema_version') != 1:
        raise ValueError('Only schema_version 1 rigs can be converted')
    specs = {entry['profile_id']: spec for spec, entry in library.items()}
    classes = {}
    for group, members in rig.get('groups', {}).items():
        for member in members:
            if classes.setdefault(member, group) != group:
                raise ValueError(f'{member} is in several groups; MVR allows one class')
    rows = list(dict.fromkeys(classes.get(fixture['id'], '') for fixture in rig['fixtures']))
    members = {
        row: [fixture['id'] for fixture in rig['fixtures'] if classes.get(fixture['id'], '') == row]
        for row in rows
    }
    scene = mvr.new_scene()
    for group in rig.get('groups', {}):
        mvr.class_uuid(scene, group)
    for index, source in enumerate(rig['fixtures']):
        if unknown := set(source) - FIELDS:
            raise ValueError(f'{source["id"]}: unknown fields {sorted(unknown)}')
        spec = specs[source['profile']]
        if spec not in scene.entries:
            mvr.embed_gdtf(scene, spec, library[spec]['gdtf_xml'])
        group = classes.get(source['id'], '')
        row, column = rows.index(group), members[group].index(source['id'])
        position = [
            (column - (len(members[group]) - 1) / 2) * FIXTURE_SPACING_MM,
            -row * ROW_SPACING_MM,
            0.0,
        ]
        rotation = [
            source.get('pan_offset_degrees', 0.0),
            source.get('tilt_offset_degrees', 0.0),
            0.0,
        ]
        identity = mvr.stable_uuid(rig_name, 'fixture', source['id'])
        node = mvr.add_fixture(
            scene,
            name=source['id'],
            gdtf_spec=spec,
            gdtf_mode=library[spec]['default_mode'],
            universe=source['universe'],
            address=source['address'],
            fixture_id=index + 1,
            fixture_class=group,
            matrix=mvr.compose_matrix(position, rotation),
            identity=identity,
        )
        for axis in mvr.MOVEMENT_AXES:
            limits = source.get(axis, {})
            if limits.get('inverted'):
                node.insert(
                    list(node).index(node.find('FixtureID')), _flag(f'DMXInvert{axis.title()}')
                )
            if (limits.get('min', 0.0), limits.get('max', 1.0)) != (0.0, 1.0):
                mvr.set_movement_limits(
                    scene, identity, axis, limits.get('min', 0.0), limits.get('max', 1.0)
                )
    require_renderable(scene)
    return mvr.write(scene)


def _flag(tag):
    element = ElementTree.Element(tag)
    element.text = 'true'
    return element


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--profiles', type=Path, default=ROOT / 'rigs/profiles.json')
    args = parser.parse_args()
    rig = json.loads(args.input.read_text(encoding='utf-8'))
    content = convert(rig, fixture_library(args.profiles), args.input.stem)
    args.output.write_bytes(content)
    print(f'Wrote {args.output} ({len(rig["fixtures"])} fixtures)')


if __name__ == '__main__':
    main()
