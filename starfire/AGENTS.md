# Starfire

- Rust owns rendering, composition and fixture encoding; Lua authors baseline
  sequences. Autolux selects sequences, transitions and overrides. Radius owns
  audio/DMX synchronization. Keep these boundaries explicit.
- Afterglow guides the effect API; fixtures, named groups and banks organize shows.
- Live and replay use the same renderer. Supply musical time explicitly; forbid
  ambient clocks and unseeded randomness. Every sequence needs a DMX replay test.
- Selecting the active sequence never restarts it. Overrides affect only their
  properties; clearing them reveals the continuing baseline.
- Validate finite values, ranges, targets and patch bounds. Never emit reset or
  automatic-program channels from ordinary effects. Keep Lua work bounded.
- Use named domain constants, small interfaces, rustfmt and Clippy. No unsafe
  code, speculative frameworks, scaffolding or tooling inside runtime modules.
- Record decisions and open questions in `../docs/starfire/decisions.md` before
  implementing them. Keep tests here and build/development helpers in `../tools/`.
