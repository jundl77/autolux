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
address. Permit UDP 19002 from the controller for the Radius bus.

Keep the private key on the controller at `~/.ssh/autolux_radius_ed25519`.
Verify the receiver's public SSH host key through a trusted connection and store
it in `~/.ssh/known_hosts_autolux`. Production requires strict host-key checking.
`--receiver-user`, `--ssh-key` and `--known-hosts` select other settings.

The receiver config selects its physical speaker endpoint. SCP uploads versioned
binary/DLL bundles under the receiver user's local application-data directory.
SSH runs one supervised receiver session; no Python installation is required on
the receiver. Concurrent receiver sessions are refused. Session logs are retained.

The default dashboard is port 8765, with Prometheus at `/metrics`. Headless mode
serves metrics on port 8766. Use `--duration 90` for a bounded live test.

Starfire uses an owned subprocess with framed protobuf over its pipes. Model
preparation precedes audio capture; heartbeat requests and replies run every ten seconds,
with a thirty-second deadline. `--rig`, `--sequences` and `--starfire-binary`
select the fixture patch, Lua catalogue and executable. Art-Net goes to the
configured Radius TX ingress; the receiver alone drives the physical fixtures.

The dashboard has Control for the timeline and essential performance, Telemetry
for detailed graphs, and Config for fixture setup. Renderer FPS is measured from
frame-counter changes; it is separate from USB updates and physical DMX refresh.
