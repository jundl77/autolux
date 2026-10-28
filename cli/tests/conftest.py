from xml.etree import ElementTree

import pytest

from autolux.lighting import mvr
from autolux.lighting.gdtf import fixture_library


@pytest.fixture
def write_rig(tmp_path):
    def write(
        library, name, fixture_class='', rotation=(0.0, 0.0, 0.0), invert_pan=False, mode=None
    ):
        [(spec, profile)] = fixture_library(library).items()
        scene = mvr.new_scene()
        mvr.embed_archive(scene, spec, profile['archive'])
        node = mvr.add_fixture(
            scene,
            name=name,
            gdtf_spec=spec,
            gdtf_mode=mode or profile['default_mode'],
            universe=0,
            address=1,
            patch_number=1,
            fixture_class=fixture_class,
            matrix=mvr.compose_matrix([0.0, 0.0, 0.0], rotation),
        )
        if invert_pan:
            invert = ElementTree.Element('DMXInvertPan')
            invert.text = 'true'
            node.insert(list(node).index(node.find('FixtureID')), invert)
        rig = tmp_path / 'rig.mvr'
        rig.write_bytes(mvr.write(scene))
        return rig

    return write
