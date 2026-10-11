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
`{v, npub, port}` (this node's FIPS npub and the FIPS UDP port the tunnel
feeds), and keeps one Reticulum `Link` per remote tunnel.

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

## Security notes

* The FIPS native API socket is the authorization boundary: any process that
  can open it can send as the node. Run `rnsd` as a user that should have that
  power, and nobody else.
* Reticulum frames are not inspected by the bridge; their end-to-end
  cryptography is untouched. FIPS encrypts the bridged hop again.
* HELLO is not authenticated by the bridge; it does not need to be. The FIPS
  flow authenticates the peer npub, and Reticulum authenticates its own frames.
  `allow`/`peers` decide which npubs may become Reticulum peers at all.
