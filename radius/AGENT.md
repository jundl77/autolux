# Radius development

Keep one TX and one RX. All remote media uses the versioned bus protocol and its
64-bit session/watermark fields. Parse lengths before accessing payloads; stay
below the datagram MTU budget.

Audio callbacks use preallocated SPSC queues. Producer and consumer own separate
cursors. Never allocate, log, lock or perform network/USB operations there.
Bound overflow, underflow, loss concealment and latency recovery, and count each.

DMX output has its own worker. Schedule against the audible audio watermark;
keep show delay separate from jitter buffering. Stop stale output explicitly.
Heartbeat both ways every ten seconds and send receiver health every second.

Run `.\build-radius.ps1` from the root. Tests must cover packet bounds, queue
ownership, clock drift, loss, overload, heartbeat expiry and process lifecycle.
