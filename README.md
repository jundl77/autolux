# Autolux

Autolux turns live music into synchronized audio and programmable lighting.
One command runs analysis, deploys the receiver and monitors the complete show.

| Directory | Responsibility |
| --- | --- |
| `cli/` | Startup, SSH deployment, supervision and shutdown |
| `autolux/lib/` | Music analysis and lighting decisions |
| `autolux/lighting/` | Starfire control, report replay and fixture storage |
| `autolux/simulate/` | Replay, dashboard and evaluation |
| `autolux/training/` | Data, models, labeling, training and backtests |
| `radius/` | C++ audiovisual transport: `radius_tx` and `radius_rx` |
| `starfire/` | Rust/Lua lighting renderer, fixture setup and deterministic replay |
| `rigs/` | Shared fixture patches and DMX profiles |
| `proto/` | Starfire control and Radius transport protobuf contracts |
| `tools/` | Development checks and diagnostics |
| `docs/` | Architecture, decisions and operations |

Each component keeps its own `tests/` directory.

## Setup

Windows, Python 3.11, uv and Visual Studio C++ Build Tools are required.
Install Rust through rustup for Starfire; `rust-toolchain.toml` selects its version.

```powershell
git submodule update --init --recursive
uv sync --extra dev --extra visualizer
uv run pre-commit install
.\build.ps1
```

Keep models and music under `autolux/training/data/raveform/`, or set
`RAVEFORM_DATA_DIR`. Production requires the shipped model.

Route music to **CABLE Input**; analysis and Radius both capture **CABLE Output**.
The receiver needs speakers, the Enttec USB device and key-based Windows SSH.
See [receiver setup](cli/README.md). Fixture patches and profiles live in `rigs/`;
named Lua sequences live in `starfire/sequences/`. Saved fixture edits apply on
the next session.

## Run

```powershell
uv run autolux --prod --check
uv run autolux --prod
uv run autolux status
uv run autolux stop
```

The CLI copies the receiver bundle over SCP, starts it over SSH, then starts
Radius TX, Starfire and Autolux. Ctrl-C stops the owned session at both ends. RDP is not
needed during operation. Config templates live beside the two Radius apps;
`--tx-config`, `--rx-config` and `--receiver-host` override them.

Open **http://autolux.local:8765** for analysis, lighting and system health.
The dashboard binds to `0.0.0.0`; mDNS publishes the local hostname while it runs.
Allow TCP 8765 and UDP 5353 from your local network in Windows Firewall.
If a device cannot resolve mDNS, use the controller PC’s LAN IP with port 8765.
Prometheus metrics are at `/metrics` on the same address. `--no-ui` keeps metrics
available on port 8766. Logs and generated configs are under `.live/`.

## Verify

`uv run autolux --prod --check` checks configurations, binaries, models, capture,
fixtures, sequences and SSH without starting a show. Readiness has a 45-second timeout.
Use `uv run autolux --prod --check-full` for formatting, builds, the full test
suite, committed backtest, simulation and readiness. Full validation takes several
minutes and writes results under `.live/validation/`.
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
See [Starfire](starfire/README.md) for programmable lighting replay and fixture
setup, and the [system design](docs/starfire-design.md) for timing and protocols.

## Formatting

Build scripts format Python with Ruff, C++/Protobuf with clang-format and Rust
with rustfmt. Run `uv run pre-commit run --all-files` to apply saved policies across the
repository. Commit hooks format staged source files; re-stage any changes they make.

Python uses four spaces, a 100-column target and preserved quotes. C++ uses tabs
at width four, a 120-column target, expanded function/control braces and blank
lines between definitions. Settings live in `pyproject.toml`, `.clang-format`
`.editorconfig` and `rustfmt.toml`. Dependencies, generated files and training data
are excluded. Starfire's build also runs Clippy with warnings denied.
