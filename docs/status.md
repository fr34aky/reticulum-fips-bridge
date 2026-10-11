# Status and verification log

## 2026-10-11: both directions verified live

Two real nodes: a home host and a cloud VM, each running FIPS 0.6.0-dev
(master, rev 7ae9355) with the native API enabled, Reticulum 1.5.7 via
`rnsd`, and `rnsfips` 0.1.0. The two FIPS nodes peer over the public FIPS
mesh.

### Phase 1: Reticulum over FIPS

- `FIPSInterface` on both rnsd instances, configured with each other's npub.
- `rnstatus` on both sides shows `FIPSPeerInterface[FIPS/npub1...]` up at
  MTU 1169 after the HELLO exchange.
- `rnpath` to an `rncp` listener on the VM resolves to 1 hop via the FIPS
  peer interface.
- A 300 KB `rncp` transfer completes at about 1.3 Mbps; the SHA-256 of the
  received file matches the sender's.

### Phase 2: FIPS over Reticulum

- A second, isolated FIPS daemon on the host (loopback UDP only, no peers, no
  Nostr or LAN discovery) runs `rnsfips tunnel` against its control socket.
  Its only path to anything is the tunnel.
- The VM runs `rnsfips tunnel` against its normal FIPS daemon with the host
  tunnel's destination hash in `peers`.
- After the Reticulum link comes up, both tunnels exchange HELLOs and tell
  their FIPS daemons to peer over the loopback shim. The isolated daemon shows
  the VM as a connected peer at `127.0.0.1:<shim port>`, takes spanning-tree
  depth 2 and estimates the mesh at about 900 nodes.
- `fipsctl probe` from the isolated daemon to a third mesh node (the host's
  main FIPS daemon) succeeds: bloom hit, discovery answered on the first
  attempt, Noise XK session established, 132 ms round trip, observed path MTU
  1280 bytes. The Reticulum link's MDU was about 1071 bytes, so the bridge's
  fragmentation carried full-size FIPS frames in live traffic.
- In this test the Reticulum link between the two rnsd instances itself ran
  over the Phase 1 FIPS interface (FIPS over Reticulum over FIPS). The
  isolated daemon never saw anything but a loopback UDP peer.

### Phase 2, second run: three real nodes, VM reachable only over Reticulum

- A third machine (FIPS node on the public mesh, rnsd with a TCP client
  interface to the VM's rnsd, `rnsfips tunnel` with `auto_peer = yes`) learned
  the VM's tunnel from one `rnsfips tunnel-ctl announce` on the VM and dialled
  it; the link came up with MDU 16303 over the TCP hop.
- The VM's FIPS daemon was then stripped of static peers and Nostr rendezvous,
  so its only peer is the third machine's npub via `127.0.0.1:<shim port>`
  (`direction: inbound`, `is_parent: true`, tree depth 3).
- `fipsctl probe` from the VM to the home node's main FIPS daemon: session
  already present, 43 ms round trip, four-hop tree route through the tunnel
  peer, path MTU 1280. The VM joined the mesh with no FIPS transport but the
  Reticulum tunnel.
- Tunnel counters on both ends climb with FIPS heartbeats and bloom announces
  (tx 150 / rx 198 on the VM after a few minutes, no drops).

### Phase 1 over Phase 2: Reticulum over FIPS over Reticulum over FIPS

- With the VM's FIPS reachable only through the third machine's tunnel, the
  VM's `[[FIPS]]` interface was re-enabled. The home bridge's native flow to
  the VM now crosses the public mesh to the third machine and the tunnel into
  the VM; FIPS discovery resolved the VM at depth 4 and the flow came up on
  the first HELLO exchange.
- Reticulum links from the home node to both VM destinations establish in
  about 50 ms, link RTT 46 to 49 ms, link MTU 1169 (MDU 1087), unchanged from
  the direct-peer case apart from a few milliseconds.
- A 300 KB `rncp` transfer from the home node to a listener on the VM over
  this path completed at 2.18 Mbps (the direct-peer Phase 1 run did 1.3 Mbps;
  the public mesh hop to the third machine is simply a better route than the
  old direct UDP peering was).
- Pitfall found on the way: a `[[FIPS]]` block whose `peers` line is
  commented out starts with "0 configured peer(s)" and silently refuses the
  other side's flow. Since commit 40c1e1e the refusal is a warning.

### Phase 3: operations

- `rnsfips status` identified a hollow rnsd on the VM (started beside a
  running instance, left as a client) within one run.
- Announce-based discovery: after the 0.1.0 code was deployed on both sites,
  each listed the other as a discovered bridge within a minute without any
  configuration beyond the original `peers` entry.
- A restart of either rnsd now recovers within a second on the other side
  (nonce-based flow replacement), verified on the live pair.

### Deployment notes learned

- `rnsd` must run in the same Python environment as `rnsfips`; Reticulum
  imports the interface inside its own interpreter. With pipx: `pipx install
  rnsfips` then `pipx inject rnsfips rns --include-apps`, and do not keep a
  separate `rns` pipx venv on PATH. `rnsfips install-interface` now warns when
  the `rnsd` on PATH cannot import `rnsfips`.
- `~/.reticulum/config` is created by the first `rnsd` run (or
  `rnsd --exampleconfig`); set `enable_transport = yes` and add one `[[FIPS]]`
  block. Duplicate section names are a parse error.
- The tunnel config is INI at `~/.config/rnsfips/tunnel.conf`.
- A FIPS node will not dial a peer it already holds live, so a tunnel between
  two nodes that already peer directly sits idle. Use the tunnel for nodes
  that have no other way in.
- `rnsfips install-units` writes systemd user units; enable lingering so they
  outlive the login session. Disable the tunnel unit on hosts with no
  `tunnel.conf`. Stop a hand-started rnsd before enabling the unit: an rnsd
  started next to a running instance becomes a client of it and stays a
  (useless) client after the other exits; restart the unit if that happened.
- Reticulum ignores a re-announce emitted in the same second as the previous
  one, and its logs are block-buffered when stdout is a file
  (`PYTHONUNBUFFERED=1`).
- `drop_no_port` in `fipsctl show native-flows` counts every datagram that
  reaches the node for a port nobody holds, from any peer. On the public mesh
  other nodes' native-API traffic lands there too (seen: a third node sending
  to port 7368 every few seconds). A growing counter is only a bridge problem
  if `rnsfips status` also shows a peer down or a flow missing; the daemon's
  `fips::node::handlers::session=debug` filter names the source and port.
- After an rnsd restart the surviving side replaces the dead flow as soon as
  the restarted peer's HELLO (new nonce) arrives; before that fix it waited
  out the 60 s silence timeout while the restarted side sent into the void.
- FIPS builds before commit 55fb970 cannot complete discovery for a native
  flow to a multi-hop peer without the key in their identity cache; the
  interface's `fips_dns` priming covers that and is harmless on master.
