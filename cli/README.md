# Autolux CLI

`autolux --prod` coordinates the show from the controller PC. It validates
configuration, prepares analysis models, deploys Radius RX, starts capture and Starfire, and
cleans up both endpoints on shutdown. `autolux status` reports health;
`autolux stop` requests a graceful stop.

`autolux --prod --check` checks readiness without starting outputs and times out
after 45 seconds. `--check-full` also runs builds, tests, backtest and simulation.

## Receiver setup

Install Windows OpenSSH Server once on the receiver. From an elevated PowerShell,
run `deployment/enable-ssh.ps1` with the controller's Ed25519 **public** key and
controller IPv4 address. The script enables key access and restricts SSH to that
address. The launcher permits TCP 19002 from that controller for the Radius bus.

Keep the private key on the controller at `~/.ssh/autolux_radius_ed25519`.
Verify the receiver's public SSH host key through a trusted connection and store
it in `~/.ssh/known_hosts_autolux`. Production requires strict host-key checking.
`--receiver-user`, `--ssh-key` and `--known-hosts` select other settings.

The receiver config selects its physical speaker endpoint. SCP uploads versioned
Rust receiver bundles under the receiver user's local application-data directory.
SSH runs one supervised receiver session; no Python installation is required on
the receiver. Concurrent receiver sessions are refused. Session logs are retained.

The default dashboard is port 8765, with Prometheus at `/metrics`. Headless mode
serves metrics on port 8766. Use `--duration 90` for a bounded live test.

Starfire uses an owned subprocess with framed protobuf over its pipes. Autolux
loads the MVR rig, its embedded GDTF modes and Lua sources into Login; LoginReply
confirms readiness. Model preparation precedes audio capture. Independent heartbeats
run every ten seconds with a thirty-second deadline. `--rig`, `--autoloops` and
`--starfire-binary` select the MVR rig, Lua catalogue and executable; `--profiles`
selects the fixture library offered in Config. Starfire passes complete DMX frames directly to
its Radius TX worker. One TCP connection carries audio, lights and telemetry;
RX uses a 500 ms buffer and schedules lighting against audible audio.
Standalone Starfire can also output Art-Net.

`autolux starfire import-fixture fixture.gdtf --profile-id my_fixture --mode "Standard"`
validates and stores a GDTF mode in the `rigs/profiles.json` library. Config lists
imported profiles for patching and embeds the GDTF in the MVR when a fixture uses it.
Imports replace the selected profile ID atomically; no native rebuild is needed.
Unsupported modes are rejected before saving. A `.live/starfire/rig.json` is refused;
the rig is `.live/starfire/rig.mvr`.

The dashboard has Control for the timeline and essential performance, Telemetry
for detailed graphs, and Config for fixture setup. Renderer FPS is measured from
frame-counter changes; it is separate from USB updates and physical DMX refresh.
