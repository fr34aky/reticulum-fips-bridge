# Changelog

## 0.1.4 (2026-10-11)

- Tunnel: one link per pair. A tunnel no longer dials a remote whose own link
  to it is up, and when both dials race the link initiated by the smaller
  destination hash survives on both sides. Listing each other in `peers` on
  both sides used to leave two live links.
- Tunnel: a tunnel's own destination hash in `peers` is skipped with a
  warning; `add_peer` rejects it.
- Interface: a refused inbound flow (npub not in `peers`/`allow`, and
  `accept_unknown` off) is logged as a warning, once per peer per 10 minutes,
  instead of a debug line.
- README rewritten around the two use cases, with one walkthrough each and
  the pitfalls met during the three-node deployment.

## 0.1.3 (2026-10-11)

- Tunnel: a second `rnsfips tunnel` on the same control socket path now exits
  with a clear message instead of silently taking the socket over from the
  running one (which left `rnsfips status` with "connection refused" once the
  second instance was killed). A stale socket file left by a killed tunnel is
  still replaced.
- Tunnel: SIGTERM and SIGHUP shut down cleanly and remove the control socket,
  so `systemctl stop` no longer leaves a stale file.
- `rnsfips status` explains a stale control socket instead of printing the
  raw `ConnectionRefusedError`.
- README: install from the checkout; `rnsfips` is not on PyPI.

## 0.1.2 (2026-10-11)

- Tunnel: the responder sends its HELLO only after the initiator has
  identified and been admitted. Before, a refused initiator briefly saw the
  tunnel as up and told its FIPS daemon to dial, then flapped on every retry.

## 0.1.1 (2026-10-11)

- `rnsfips tunnel` waits for rnsd's shared instance and requires it. A tunnel
  restarted together with rnsd (the unit has `Requires=rnsd.service`) could
  win the start race, become the shared instance itself and leave rnsd as a
  client serving nothing. `standalone = yes` keeps the old behaviour for hosts
  without rnsd.

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
