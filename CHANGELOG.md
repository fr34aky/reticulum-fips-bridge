# Changelog

## 0.1.0 (2026-10-11)

First release. Both bridge directions, verified live between two sites on the
public FIPS mesh.

- `FIPSInterface`: Reticulum interface over the FIPS native datagram API.
  Per-peer spawned interfaces, HELLO/PING liveness, nonce-based replacement
  of a restarted peer's flow, dedupe of simultaneous dials, link MTU of about
  1169 bytes across the FIPS hop, identity-cache priming through the FIPS
  `.fips` resolver for older FIPS builds.
- Bridge discovery over Reticulum announces (`rnsfips.bridge`) with gossip of
  known bridges and optional `auto_peer`.
- `rnsfips tunnel`: FIPS over Reticulum links with bridge-level
  fragmentation, loopback UDP shim driven through the FIPS control socket,
  identified inbound links with an allow list, control socket and
  `tunnel-ctl`.
- `rnsfips status`: combined FIPS, Reticulum and tunnel view.
- `rnsfips install-interface` and `rnsfips install-units` (systemd user or
  system units, unbuffered logging).
- Tests: fake FIPS daemon, subprocess Reticulum sites, multi-site and tunnel
  scenarios; Docker lab with a real three-node FIPS mesh and an rncp transfer
  smoke test; CI on push.
- Docs: stack comparison, design, live verification log and deployment notes.
