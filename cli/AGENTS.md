# Coordinator Python

- Follow the Python naming and clean-code rules in `../autolux/AGENTS.md`.
- Own only processes started by this session. Validate configuration before
  deployment; verify readiness, unwind partial starts and bound shutdown.
- Name timeout, retry and protocol constants with units. Keep credentials out
  of logs and configuration committed to Git.
- This package owns lifecycle and deployment. Development helpers belong in
  root `tools/`; lifecycle tests belong in `cli/tests/`.
