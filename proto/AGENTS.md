# Shared protocols

- Keep shared schemas here; generated bindings belong in build output, runtime
  framing and validation beside their consumers. Generate language bindings with
  standard Protobuf compilers.
- Maintain one current contract per component. Update both callers together;
  avoid parallel versions and compatibility layers. Keep units explicit and
  validate lengths, ranges, sessions and numeric boundaries.
- Define endpoint roles and comment every wire message with `[CLIENT -> SERVER]`
  or `[SERVER -> CLIENT]`; mark shared payloads. Use typed alternatives and enums
  for closed concepts. Initialization belongs in Login; sessions own resources.
- Follow `../radius/AGENTS.md` for C++ style, ownership, naming and regression tests.
