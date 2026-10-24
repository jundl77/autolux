# Autolux

Autolux turns live music into synchronized audio and SoundSwitch lighting.
One command runs analysis, deploys the receiver and monitors the complete show.

| Directory | Responsibility |
| --- | --- |
| `cli/` | Startup, SSH deployment, supervision and shutdown |
| `autolux/lib/` | Music analysis and lighting decisions |
| `autolux/simulate/` | Replay, dashboard and evaluation |
| `autolux/training/` | Data, models, labeling, training and backtests |
| `radius/` | C++ audiovisual transport: `radius_tx` and `radius_rx` |
| `tools/` | Development validation |
| `loopMIDI/`, `docs/` | Shared setup and documentation |

Tests live in `autolux/tests/`, `cli/tests/` and `radius/tests/`.

## Setup

Windows, Python 3.11, uv and Visual Studio C++ Build Tools are required.

```powershell
git submodule update --init --recursive
uv sync --extra dev --extra visualizer
uv run pre-commit install
.\build.ps1
```

Keep models and music under `autolux/training/data/raveform/`, or set
`RAVEFORM_DATA_DIR`. Production requires the shipped model.

Configure SoundSwitch for Performance Mode and VirtualDJ/OS2L, enable Art-Net
output to localhost, and exit MIDI mapping mode. Use **loopMIDI Port 1** and route
music to **CABLE Input**; analysis and Radius both capture **CABLE Output**.
The receiver needs speakers, the Enttec USB device and key-based Windows SSH.
See [receiver setup](cli/README.md).

## Run

```powershell
uv run autolux --prod --check
uv run autolux --prod
uv run autolux status
uv run autolux stop
```

The CLI copies the receiver bundle over SCP, starts it over SSH, then starts
Radius TX and Autolux. Ctrl-C stops the owned session at both ends. RDP is not
needed during operation. Config templates live beside the two Radius apps;
`--tx-config`, `--rx-config` and `--receiver-host` override them.

Open **http://127.0.0.1:8050** for analysis, lighting and system health.
Prometheus metrics are at `/metrics` on the same address. `--no-ui` keeps metrics
available on port 8051. Logs and generated configs are under `.live/`.

## Verify

`uv run autolux --prod --check` runs formatting checks, the native and package
builds, all tests, the full committed backtest, simulation and device/SSH readiness.
It keeps the show stopped and writes results under `.live/validation/`.
For individual checks:

```powershell
uv run pytest
.\build-radius.ps1
uv run python autolux/training/run_eval_set.py
uv run autolux simulate file path\to\track.mp3
uv run autolux label path\to\track.mp3
```

Backtests compare against committed baselines. Run GPU workloads serially.
See [operations](docs/operations.md) and [Radius](radius/README.md).

## Formatting

Both build scripts format Python and C++ before building. Run
`uv run ruff format --check autolux cli radius tools` to check Python without writing;
use `uv run clang-format --dry-run --Werror <files>` for C++.
Pre-commit hooks format staged source files; re-stage any changes they make.

Python uses four spaces, a 100-column target and preserved quotes. C++ uses tabs
at width four, a 120-column target, expanded function/control braces and blank
lines between definitions. Settings live in `pyproject.toml`, `.clang-format`
and `.editorconfig`. Dependencies, generated files and training data are excluded.
