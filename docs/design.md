# rnsfips design

## Envelope

Every datagram the bridge sends, on a FIPS flow or over a Reticulum link, starts
with one type byte. A datagram is never empty, which lets a zero-length read on a
FIPS flow mean "closed by the daemon".

| byte | name | payload |
| --- | --- | --- |
| `0x01` | HELLO | msgpack map: `v` (envelope version, 1), `impl`, `ver`, `nonce` (8 random bytes per process), `hw_mtu`, `tid` (Reticulum transport identity hash or null) |
| `0x02` | PING | empty |
| `0x03` | PONG | empty |
| `0x10` | RNS_FRAME | one complete Reticulum frame, IFAC already applied |
| `0x20` | FIPS_FRAME | one complete FIPS wire frame |
| `0x21` | FIPS_FRAG | `msg_id u16 BE, index u8, count u8`, then part of a FIPS frame |

`nonce` lets a peer detect that the other side restarted (and re-synthesize the
Reticulum tunnel so paths are restored). `hw_mtu` lets both sides agree on the
smaller of their interface MTUs.

## Reticulum over FIPS (`rnsfips.interface`)

`FIPSInterface` is the container Reticulum loads from its configuration. It
holds one FIPS native-API listener on `listen_port` and dials every configured
peer at `npub:port`. A flow becomes a `FIPSPeerInterface` once the peer's HELLO
has arrived; that child is registered with `RNS.Transport` and is what carries
traffic. The arrangement mirrors Reticulum's own `BackboneInterface` and
`WeaveInterface`.

Threads:

* I/O thread: a `selectors` loop over the listener, every flow and a wakeup
  socket. All flow state changes happen here.
* Supervisor thread: the blocking setup calls (`listen`, `connect`) against
  the daemon, with exponential backoff per peer, handing results to the I/O
  thread.

Lifecycle rules that follow from Reticulum's loader:

* Children are spawned only after `final_init`, because Reticulum applies
  `mode`, IFAC, announce settings and `OUT` to the parent after construction.
* A child copies the parent's ingress/egress control, IFAC, announce rate,
  mode, gravity and announce cap, exactly the set `BackboneInterface` copies;
  Transport and `rnstatus` read these unconditionally.
* The parent's `process_outgoing` is a no-op. Transport broadcasts to any
  `OUT and online` interface, and the parent is both so that `rnstatus` shows it
  up; unicast traffic follows path-table entries, which point at children.
* Children have distinct `name`s (attach/detach keys a dict by name) and a
  `__str__` that is stable across reconnects (it feeds `get_hash()`, which
  Reticulum uses for tunnel ids).
* Configured peers keep their child registered across reconnects, like a
  Reticulum TCP/Backbone client; after a reconnect the child calls
  `RNS.Transport.synthesize_tunnel` so the far side restores paths. Inbound
  peers are removed from Transport when their flow dies.
* If both sides dial each other, the flow dialled by the lexicographically
  smaller npub wins and the other is closed.

MTU: `HW_MTU = max_payload - 1 - ifac_size`, where `max_payload` is reported
per flow by FIPS (1170 at FIPS's 1280-byte default) and the IFAC bytes count
only when an access code is configured. `FIXED_MTU = True` is what allows
Reticulum's link MTU discovery to raise links across this hop above 500 bytes;
`AUTOCONFIGURE_MTU = False` keeps `optimise_mtu` from overriding it. Both sides
exchange their value in HELLO and use the minimum. Announces remain bounded by
Reticulum's 500-byte MTU.

Liveness: the I/O thread PINGs an idle flow every `ping_interval` and declares
it dead after `peer_timeout` without any datagram. Flows that never send HELLO
are dropped after 10 s.

Identity priming: before dialing a peer the supervisor resolves `<npub>.fips`
at the daemon's own DNS responder (`fips_dns`, default `[::1]:5354`). The
daemon registers the resolved key in its identity cache, which FIPS builds
before commit 55fb970 require to verify the discovery proof for a native flow
to a node more than one hop away. Newer builds cache the key from the
`connect` command itself; the extra query is then harmless.

Peer restarts: HELLO carries a per-process nonce. When a HELLO from a known
npub arrives with a different nonce, the peer's rnsd has restarted and its
old flow is dead on their side, so the new flow replaces the old one at once,
whichever side dialled it. Only with an equal nonce (both sides dialled each
other at the same time) does the dedupe rule apply: the flow dialled by the
lexicographically smaller npub wins. While a flow is pending, HELLO is
re-sent every 3 s for up to 30 s, because datagrams are unreliable, the far
listener may not exist yet, and FIPS session setup with a discovery retry
ladder can take about 20 s.

### Discovery (`rnsfips.discovery`)

Each interface announces a Reticulum destination `rnsfips.bridge` whose
`app_data` is msgpack `{v, n: key, p: port, t: transport id, b: [[key, port], ...]}`:
its own FIPS npub (32-byte x-only key), the FIPS port it holds, and up to six
recently seen bridges (gossip). Announces go out at `announce_interval`
(default 1 h) and about 30 s after a new peer comes up, so a newcomer learns
the rest of the bridges from the first peer's announce. With `auto_peer = yes`
learned bridges are dialled over FIPS like configured ones and accepted
inbound; without it they are only listed (`rnsfips status`, state file
`storage/rnsfips_bridges.json`). Discovery runs over Reticulum, so it needs one
existing Reticulum path; it never replaces the first configured peer.

## FIPS over Reticulum (`rnsfips.tunnel`)

`rnsfips tunnel` is a Reticulum shared-instance client: it waits for rnsd's
shared instance and starts with `require_shared_instance`, so it can never
become the instance itself when restarted alongside rnsd (`standalone = yes`
opts into an own instance on hosts without rnsd). It registers the
destination `rnsfips.tunnel`, announces it with `app_data` = msgpack
`{v, npub, impl, ver}` (this node's FIPS npub, implementation name and
version), and keeps one Reticulum `Link` per remote tunnel.

Per remote tunnel the daemon binds a UDP socket on `127.0.0.1` and asks the
local FIPS daemon over its control socket to `connect` to the remote npub at
that address. FIPS then runs its ordinary Noise IK handshake and everything
above it through the tunnel. Datagrams from FIPS are sent over the link as
`FIPS_FRAME` when they fit the link MDU, else as `FIPS_FRAG` fragments
reassembled on the far side (5 s timeout, best effort). Datagrams from the link
are written to the UDP socket toward FIPS.

The local UDP transport instance stays at FIPS's default 1280-byte MTU so a
tunnel never lowers the node-wide MTU; fragmentation is the bridge's job. Over
a Reticulum path whose link MDU is below about 1100 bytes, a FIPS bloom announce
costs three Reticulum packets. On Backbone/TCP-class paths with link MTU
discovery it fits in one.

The tunnel exposes a JSON-line control socket
(`$XDG_RUNTIME_DIR/rnsfips/tunnel.sock`, `control_socket` in `tunnel.conf`)
with `status`, `announce`, `add_peer` and `remove_peer`, used by `rnsfips
status` and `rnsfips tunnel-ctl`. Inbound links must identify themselves
(`Link.identify`) and are admitted by the `allow` list before their HELLO is
processed and before the responder sends its own HELLO, so a refused
initiator never sees the tunnel as up.

FIPS's control plane assumes IP-class links. Over LoRa-class Reticulum paths,
set `node.mmp.mode: minimal` and raise `node.link_dead_timeout_secs` on the FIPS
side, and expect it to be slow. This mode is experimental.

## Discovery: who finds whom

Two different things get discovered, and it helps to keep them apart. The
bridge's own announces only answer "which other bridges exist, and should I
dial them". Finding the nodes *behind* a bridge is left entirely to the native
mechanism of the network being extended, which never learns that a bridge is
involved. In both directions exactly one configured entry per new site is
needed to obtain a first Reticulum path; everything after that is automatic.

### Reticulum nodes across FIPS

1. **Bridges find each other** through the `rnsfips.bridge` announce
   described above. Because it is an ordinary Reticulum announce it propagates
   through every transport node in the connected Reticulum network, over FIPS
   hops and non-FIPS hops alike, so every bridge hears every other bridge's
   own announce directly. The six-entry gossip list is an accelerator for
   newcomers, not a limit on how many bridges can be known.
2. **What a bridge does with a learned bridge** is a local policy decision.
   With `auto_peer = yes` it dials the npub over FIPS and accepts that npub's
   inbound flow; otherwise the entry is only recorded for `rnsfips status`,
   and flows are admitted solely by `peers`, `allow` and `accept_unknown`.
   With `auto_peer` on everywhere, N bridges converge to a full mesh of direct
   FIPS flows, one per pair after dedupe. With it off, traffic between two
   sites that are not configured with each other still works, but transits
   the bridge they are both configured with, as two FIPS hops.
3. **Reticulum nodes behind a bridge** need nothing from rnsfips. Once a
   `FIPSPeerInterface` is up it is a regular interface on an rnsd with
   `enable_transport = yes`. Destination announces from the far site cross it
   like any TCP or LoRa hop, path requests and links follow the usual rules,
   and an application sees only a path with one more hop.

Freshness: a bridge is normally known everywhere within a minute of starting
(announce on start, re-announce about 30 s after a new peer comes up), but a
bridge that disappears stays in the discovered list until its entry ages out.

### FIPS nodes across Reticulum

1. **Tunnels find each other** through the `rnsfips.tunnel` announce. Each
   `rnsfips tunnel` daemon announces its destination with its FIPS npub on
   start and every `announce_interval` (default 30 min), listens for the
   same aspect, and records every tunnel it hears for `rnsfips status`.
   `peers` lists destination hashes it dials itself; `allow` (hashes or
   `any`) lists who may link in; with `auto_peer = yes` a learned tunnel that
   is also allowed is dialled automatically. Two tunnels bring up a Reticulum
   link, identify to each other, exchange HELLOs carrying their npubs, and the
   link becomes the pipe.
2. **FIPS is handed a UDP peer, not a Reticulum path.** For each remote
   tunnel the daemon binds a local UDP port on `127.0.0.1` and asks its own
   FIPS daemon over the control socket to `connect` to the remote npub at
   that address. FIPS sees one more directly connected UDP peer with a known
   npub and runs its Noise IK handshake, heartbeats, spanning tree and bloom
   announces over it unchanged.
3. **FIPS nodes beyond that peer** are found by FIPS's own control plane.
   The bridged peer joins the spanning tree and exchanges bloom filters, so
   the node reaches any mesh member by the ordinary bloom lookup and Noise XK
   session, through its tunnel peer and onward over real FIPS transports.
   This is what the Phase 2 live test exercised: an isolated daemon whose
   only peer was the tunnel probed a third mesh node successfully.

Practical limit: FIPS will not dial a peer it already holds a live session
with, so a tunnel between two FIPS nodes that already peer directly over the
internet sits idle. The tunnel is for FIPS nodes whose only route out is
Reticulum, for example behind a LoRa or serial link.

### Summary

| | Reticulum over FIPS | FIPS over Reticulum |
| --- | --- | --- |
| Bridge-to-bridge discovery | `rnsfips.bridge` announce + gossip, `auto_peer` | `rnsfips.tunnel` announce, `peers`/`allow`/`auto_peer` |
| What the extended network gains | one more Reticulum interface | one more FIPS UDP peer on loopback |
| Who finds the far nodes | Reticulum announces and path requests | FIPS spanning tree and bloom routing |
| Minimum static config per new site | one npub in `peers` | one destination hash in `peers` (or `allow` + a peer that dials) |

## Routing loops

Each stack is loop-free on its own, and the bridge adds no forwarding
decision of its own, so no packet can circle. The reasoning, checked against
the sources:

**Reticulum.** Frames carry a hop counter and are rejected at 128 hops; every
transport node keeps a hash of recently seen packets and drops duplicates;
data is forwarded to a single next hop from the path table; an announce is
retransmitted at most once and that retransmission is suppressed as soon as a
rebroadcast is heard. A Reticulum frame crossing the FIPS hop is just a frame
on one more interface, subject to all of the above.

**FIPS.** Forwarding requires strict progress in tree coordinates (`find_next_hop`
only hands a datagram to a peer that is closer to the destination), session
datagrams carry a TTL of 64, and lookups are deduplicated. A FIPS frame crossing
the Reticulum hop is one more link to one peer, subject to the same rules.

**Reticulum over FIPS.** FIPS is a pure link layer here: each datagram goes to
one named npub and FIPS never forwards Reticulum frames, so it cannot add a
Reticulum hop. If both sides dial each other and the dedupe ever failed, the
result would be two parallel peer interfaces to the same node, which Reticulum
treats as two paths; the stale one times out. Not a loop.

**FIPS over Reticulum.** The tunnel is one more FIPS link; tree routing and the
TTL still govern it. Link packets are neither acknowledged nor retransmitted,
so there is no retransmission feedback between the layers either.

**The layered case.** Running FIPS over Reticulum over FIPS is safe as long as
one FIPS identity is never both carrying and carried by the same Reticulum
link. Two things enforce it. A FIPS node will not dial a peer it already holds
live (the control socket's `connect` on a live peer reports "peer live, not
dialled"), so a tunnel between two nodes that already peer directly sits idle
rather than forming a second, circular link. And in the live test the tunnelled
daemon was a separate FIPS identity from the daemon carrying the Reticulum
link, so no node ever had itself as a peer. This is why the tunnel unit is
disabled on a host whose main FIPS daemon already peers with the far side.

**Bridge announces.** The `rnsfips.bridge` discovery announce is an ordinary
Reticulum announce and crosses the FIPS hop like any other; Reticulum's
rebroadcast suppression and per-destination rate limits apply, and the
interface re-announces at most once per 30 s on peer events. It cannot storm.

**What can still happen** is not looping but churn:

- Path flapping when a site has two 1-hop Reticulum paths to the same peer
  (for example a TCP interface and the FIPS interface): re-announces may switch
  between them. Bias with the interface `gravity` option.
- Announce volume when two large Reticulum networks are joined over FIPS. It is
  bounded by `announce_cap` and the per-destination rate target; use
  `mode = gateway` or `boundary` instead of `full` for big joins.
- Overhead amplification if the bridge is chained: a 1280-byte FIPS frame
  becomes two or three Reticulum packets, each inside a 1170-byte FIPS
  datagram, roughly three times the bytes on the inner path. A cost, not a
  loop.

## Security notes

* The FIPS native API socket is the authorization boundary: any process that
  can open it can send as the node. Run `rnsd` as a user that should have that
  power, and nobody else.
* Reticulum frames are not inspected by the bridge; their end-to-end
  cryptography is untouched. FIPS encrypts the bridged hop again.
* HELLO is not authenticated by the bridge; it does not need to be. The FIPS
  flow authenticates the peer npub, and Reticulum authenticates its own frames.
  `allow`/`peers` decide which npubs may become Reticulum peers at all.
