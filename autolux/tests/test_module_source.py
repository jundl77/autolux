"""An offline script may not let import order choose its decoder."""

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from autolux.training import module_source

REPO = Path(__file__).resolve().parents[1]
DECOY_NN = {
    "__init__.py": "",
    "decoder.py": textwrap.dedent("""
        import dataclasses

        DECOY = True

        @dataclasses.dataclass(frozen=True)
        class DecodeParams:
            lag_bars: int = 2

        class FixedLagViterbi:
            def __init__(self, *args, **kwargs):
                pass

        def temper(posterior, temperature):
            return posterior
        """),
    "evaluate_v1.py": "def write_json(path, document):\n    pass\n",
    "priors.py": textwrap.dedent("""
        MODELS_DIR = "models"
        PRIORS_FILE = "priors.json"

        class Priors:
            @staticmethod
            def load(path):
                raise NotImplementedError
        """),
}


def _decoy_checkout(tmp_path):
    """A second checkout offering exactly the names the script imports.

    Import-compatible on purpose: a decoy that raised ImportError would be
    caught by any run, and the defect is the one that raises nothing.
    """
    nn = tmp_path / "decoy" / "training" / "nn"
    nn.mkdir(parents=True)
    for name, body in DECOY_NN.items():
        (nn / name).write_text(body, encoding="utf-8")
    return tmp_path / "decoy"


def _scripts_naming_another_checkout():
    """Scripts that both name a second checkout and import from `training.nn`.

    Naming one is not the hazard; a script that only hands the path to a
    subprocess picks nothing. Importing while two copies are reachable is.
    """
    found = []
    corpus = REPO / "training" / "data"
    for path in sorted((REPO / "training").rglob("*.py")):
        if corpus in path.parents:  # the gitignored corpus and its ops copies
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        statements = [
            node
            for node in tree.body
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        selects_external = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'experiment_checkout'
            for statement in statements
            for node in ast.walk(statement)
        )
        imports = [
            name
            for node in statements
            for name in (
                [node.module or '']
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else []
            )
        ]
        if selects_external and any(
            name in {'nn', 'training.nn'} or name.startswith(('nn.', 'training.nn.'))
            for name in imports
        ):
            found.append(path)
    return found


def test_the_pattern_is_still_worth_guarding():
    assert _scripts_naming_another_checkout()


@pytest.mark.parametrize("script", _scripts_naming_another_checkout(), ids=lambda p: p.stem)
def test_a_script_naming_another_checkout_states_which_one_it_imports(script):
    assert "module_source.require_imported_package_under_root(" in script.read_text(
        encoding="utf-8"
    ), (
        f"{script.relative_to(REPO)} puts another checkout on sys.path without "
        f"declaring which one its training.nn must come from"
    )


def test_a_decoder_already_imported_from_elsewhere_is_refused(tmp_path):
    decoy = _decoy_checkout(tmp_path)
    script = REPO / "training" / "decoder_rebirth" / "rebirth_case.py"
    program = textwrap.dedent(f"""
        import runpy, sys
        sys.path.insert(0, r"{decoy}")
        import training.nn.decoder
        runpy.run_path(r"{script}", run_name="probe")
        """)
    proc = subprocess.run(
        [sys.executable, "-c", program], check=False, capture_output=True, text=True, cwd=str(REPO)
    )
    assert proc.returncode != 0, (
        "the script accepted a training.nn that a previous import chose for it"
    )
    assert "training.nn resolved to" in proc.stderr


def test_experiment_checkout_requires_explicit_selection(monkeypatch):
    monkeypatch.delenv('AUTOLUX_PHASE_B_CHECKOUT', raising=False)
    with pytest.raises(RuntimeError, match='Set AUTOLUX_PHASE_B_CHECKOUT'):
        module_source.experiment_checkout('AUTOLUX_PHASE_B_CHECKOUT')


def test_experiment_checkout_rejects_missing_decoder_tree(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOLUX_PHASE_B_CHECKOUT', str(tmp_path))
    with pytest.raises(RuntimeError, match='must contain training/nn'):
        module_source.experiment_checkout('AUTOLUX_PHASE_B_CHECKOUT')


def test_experiment_checkout_preserves_selected_tree(tmp_path, monkeypatch):
    selected = _decoy_checkout(tmp_path)
    monkeypatch.setenv('AUTOLUX_PHASE_B_CHECKOUT', str(selected))
    assert module_source.experiment_checkout('AUTOLUX_PHASE_B_CHECKOUT') == selected.resolve()


@pytest.mark.parametrize('package', ['nn', 'training.nn'])
def test_explicit_experiment_refuses_a_previously_imported_decoder(tmp_path, package):
    selected = _decoy_checkout(tmp_path / 'selected')
    decoy = _decoy_checkout(tmp_path / 'other')
    script = (
        REPO / 'training/l9b_campaign/ng_sweep_driver.py'
        if package == 'nn'
        else REPO / 'training/phase_tracking/phase_tracking_probe.py'
    )
    decoy_import_root = decoy / 'training' if package == 'nn' else decoy
    program = textwrap.dedent(f"""
        import runpy, sys
        sys.path.insert(0, r"{decoy_import_root}")
        import {package}.decoder
        runpy.run_path(r"{script}", run_name="probe")
        """)
    proc = subprocess.run(
        [sys.executable, '-c', program],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env={
            **os.environ,
            'AUTOLUX_PHASE_B_CHECKOUT': str(selected),
            'AUTOLUX_LABEL9_CHECKOUT': str(selected),
        },
        timeout=10,
    )
    assert proc.returncode != 0
    assert f'{package} resolved to' in proc.stderr
    assert str(decoy) in proc.stderr


def test_experiment_interpreter_requires_an_existing_explicit_file(tmp_path, monkeypatch):
    monkeypatch.delenv('AUTOLUX_EXPERIMENT_PYTHON', raising=False)
    with pytest.raises(RuntimeError, match='Set AUTOLUX_EXPERIMENT_PYTHON'):
        module_source.experiment_python()
    monkeypatch.setenv('AUTOLUX_EXPERIMENT_PYTHON', str(tmp_path))
    with pytest.raises(RuntimeError, match='is not a file'):
        module_source.experiment_python()
    monkeypatch.setenv('AUTOLUX_EXPERIMENT_PYTHON', sys.executable)
    assert module_source.experiment_python() == Path(sys.executable).resolve()
