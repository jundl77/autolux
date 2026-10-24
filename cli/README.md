# Autolux CLI

`autolux --prod` coordinates the show from the controller PC. It validates
configuration, deploys the receiver, starts native processes and analysis, and
cleans up both endpoints on shutdown. `autolux status` reports health;
`autolux stop` requests a graceful stop.

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

The default dashboard is port 8050, with Prometheus at `/metrics`. Headless mode
serves metrics on port 8051. Use `--duration 90` for a bounded live test.
