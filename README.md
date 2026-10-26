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
| `radius/` | Rust audiovisual transport: embedded TX and remote `radius_rx` |
| `starfire/` | Rust/Lua lighting renderer, fixture setup and deterministic replay |
| `show/` | Show content: `autoloops/` (banked Lua looks), `specials/`, `palettes/` |
| `rig/` | Shared fixture patches and DMX profiles |
| `proto/` | Starfire control and Radius transport protobuf contracts |
| `tools/` | Development checks and diagnostics |
| `docs/` | Architecture, decisions and operations |

Each component keeps its own `tests/` directory.

## Setup

Windows, Python 3.11, uv, Rust and the Visual Studio MSVC build tools are required.
Install Rust through rustup; `rust-toolchain.toml` selects its version.

```powershell
uv sync --extra dev --extra visualizer
uv run pre-commit install
.\build.ps1
```

Keep models and music under `autolux/training/data/raveform/`, or set
`RAVEFORM_DATA_DIR`. Production requires the shipped model.

Route music to **CABLE Input**; analysis and Radius both capture **CABLE Output**.
The receiver needs speakers, the Enttec USB device and key-based Windows SSH.
See [receiver setup](cli/README.md). The rig is `rig/room.mvr` (MVR with
embedded GDTF, see [rig-mvr](docs/starfire/rig-mvr.md)); the fixture library is
the GDTF 1.2 archives in `rig/library/`; named Lua autoloops live in `show/autoloops/`. Saved fixture
edits apply on the next session.

The dashboard's **Config** tab provides a fixture library, a 512-channel DMX map
and an MVR rig view: a top-down plan and the full scene tree. Drag fixtures to
free addresses; click any object to edit every MVR field and the correctable GDTF
fields. Universes are displayed from 1. Draft edits run locally; only Save and
Reload contact the server. Save checks MVR conformance, renderer acceptance and
concurrent changes before replacing the rig; edits apply on the next session.

## Run

```powershell
uv run autolux --prod --check
uv run autolux --prod
uv run autolux status
uv run autolux stop
```

The CLI copies the receiver bundle over SCP, starts it over SSH, then starts
Starfire with its Radius TX worker, then Autolux. Ctrl-C stops the owned session at both ends.
RDP is not needed during operation. Config templates are `radius/transmitter.json` and
`radius/receiver.json`;
`--tx-config`, `--rx-config` and `--receiver-host` override them.

Open **http://autolux.local:8765** for analysis, lighting and system health.
The dashboard binds to `0.0.0.0`; mDNS publishes the local hostname while it runs.
Allow TCP 8765 and UDP 5353 from your local network in Windows Firewall.
If a device cannot resolve mDNS, use the controller PC’s LAN IP with port 8765.
Prometheus metrics are at `/metrics` on the same address. `--no-ui` keeps metrics
available on port 8766. Logs and generated configs are under `.live/`.

## Verify

`uv run autolux --prod --check` checks configurations, binaries, models, capture,
fixtures, autoloops and SSH without starting a show. Readiness has a 45-second timeout.
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

Build scripts format Python with Ruff, Protobuf with clang-format and Rust
with rustfmt. Run `uv run pre-commit run --all-files` to apply saved policies across the
repository. Commit hooks format staged source files; re-stage any changes they make.

Python and Rust use four spaces and a 100-column target. Settings live in
`pyproject.toml`, `.clang-format`, `.editorconfig` and `rustfmt.toml`. Dependencies,
generated files and training data are excluded. Native builds run Clippy with warnings
denied, tests and release compilation. The root build checks the Rust workspace once;
component build scripts select Radius or Starfire.
