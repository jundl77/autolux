from pathlib import Path
import pytest
from autolux.training.runtime_paths import code_component


def test_project_and_component_paths_select_the_same_source():
    component = Path(__file__).resolve().parents[1]
    assert code_component(component.parent) == component
    assert code_component(component) == component


def test_unrelated_worktree_is_rejected_instead_of_importing_installed_code(tmp_path):
    (tmp_path / 'lib').mkdir()
    with pytest.raises(ValueError, match='not an Autolux'):
        code_component(tmp_path)


@pytest.mark.parametrize('campaign', ['l9b_campaign', 'l9c_campaign'])
def test_shadow_simulation_dispatches_through_the_current_cli(campaign, monkeypatch, tmp_path):
    # Execute the real function without loading campaign models or mutating corpus data.
    import ast
    import asyncio
    import sys
    import types
    from cli import main

    source = Path(__file__).resolve().parents[1] / 'training' / campaign / 'ng_probe_rig.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    function = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_sim_inner'
    )
    root = tmp_path / 'shadow'
    config = root / 'nn_shadow/nn/decoder_config.json'
    package = types.ModuleType('nn')
    package.decoder = types.ModuleType('nn.decoder')
    package.decoder.SHIPPING_DECODER_CONFIG = str(config)
    monkeypatch.setitem(sys.modules, 'nn', package)
    monkeypatch.setitem(sys.modules, 'nn.decoder', package.decoder)
    calls = []

    async def fake_main():
        calls.append(list(sys.argv))

    monkeypatch.setattr(main, 'main', fake_main)
    monkeypatch.setattr(sys, 'path', list(sys.path))
    monkeypatch.setattr(sys, 'argv', list(sys.argv))
    monkeypatch.setenv('RAVEFORM_DATA_DIR', 'before')
    scope = {
        'Path': Path,
        'sys': sys,
        'os': __import__('os'),
        'asyncio': asyncio,
        'MAIN': source.parents[2],
        'CODE': source.parents[2],
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), scope)
    scope['run_sim_inner'](str(root), 'track.mp3', 'report.json')
    assert calls == [['autolux', 'simulate', 'file', 'track.mp3', '--report', 'report.json']]
