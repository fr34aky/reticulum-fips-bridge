# Reticulum and FIPS: synergies and differences

Facts below were taken from the Reticulum reference implementation (RNS 1.5.x,
Python) and the FIPS tree (0.6.0-dev, Rust) in October 2026. File references
point into those repositories.

## What they have in common

Both are self-organizing, permissionless, end-to-end encrypted meshes that use a
self-generated cryptographic identity as the network address, run over any
medium through a driver abstraction, and need no central infrastructure. Both
are datagram-oriented and best-effort at the network layer. Both expose a local
IPC that an external process can use without touching the stack's code:
Reticulum's shared instance and Python API, FIPS's native datagram API and
control socket. That last point is what makes a bridge possible without
modifying either project.

They complement each other: FIPS is strongest as an internet-class overlay
(UDP/TCP/Tor/Nym with NAT traversal, spanning-tree routing, 1280-byte frames),
Reticulum is strongest at the edge (LoRa, serial, packet radio, 500-byte frames,
announce-based reachability that tolerates very low bandwidth). A FIPS mesh is
a good backbone for Reticulum islands; a Reticulum network is a good last mile
for FIPS, within the limits described below.

## Side by side

| | Reticulum | FIPS |
| --- | --- | --- |
| Implementation | Python (`rns`), MicroPython/C++ (microReticulum), Go port | Rust, single crate with daemon, CLI, TUI and gateway |
| License | Reticulum License (MIT-like plus no-harm and no-AI-training conditions); protocol public domain | MIT |
| Identity | X25519 + Ed25519 keypair; addresses are 16-byte truncated SHA-256 hashes of (app name, aspects, identity) | secp256k1 Nostr keypair; mesh address is `SHA256(pubkey)[:16]`; `fd00::/8` IPv6 address derived from it |
| Addressing granularity | Per destination (one identity can own many destinations per app) | Per node, with a 16-bit port space on top |
| Routing | Announce flooding with hop counts and path tables; path requests on demand; "transport nodes" forward, others are leaves | Spanning tree with greedy coordinates, bloom-filter-guided lookups, no flooding; every node forwards |
| Hop-by-hop crypto | None (frames are opaque; optional IFAC access codes authenticate interfaces) | Noise IK per link, ChaCha20-Poly1305, periodic rekey |
| End-to-end crypto | Per-packet ECDH tokens (AES-256-CBC + HMAC), ratchets; links use ephemeral X25519 | Noise XK sessions, ChaCha20-Poly1305, periodic rekey |
| Frame size | 500 bytes fixed; links can negotiate larger MTUs when every hop supports it | 1280 bytes default, per-link; no fragmentation at any layer; 256-byte minimum actionable path MTU |
| Reliability | Links offer Channel/Buffer/Resource for reliable transfer | None natively; IPv6 adapter lets TCP do it; "ROD" planned |
| Control-plane load | Announces only when something is announced; keepalives scale with RTT (5-360 s) | TreeAnnounce, 1035-byte FilterAnnounce on change, heartbeat every 10 s, link-dead after 30 s, rekey every 120 s, MMP reports every 1-5 s |
| Driver model | `Interface` subclasses; custom ones loaded from `~/.reticulum/interfaces/*.py` at runtime | `Transport` trait, but `TransportHandle` is a closed enum built from `fips.yaml`; new transports need a fork |
| Local IPC | Shared instance (HDLC frames over Unix/TCP socket), Python API, pickle RPC | Native datagram API (JSON lines + `SCM_RIGHTS`), control socket (JSON lines), IPv6 TUN |
| Discovery | Static config, announce-based interface discovery (1.1+), mDNS-free | Static config, mDNS, Ethernet/BLE beacons, Nostr relay adverts with STUN hole punching |
| Application model | Destinations, Packets, Links, Resources; LXMF messaging on top | IPv6 applications unchanged over `fips0`; native API by `npub:port` |

## Differences that shape the bridge

**Keys are on different curves.** There is no way to derive a FIPS identity
from a Reticulum identity or vice versa. The bridge does not try. Instead a
bridge node holds both identities, and the binding between them is established
by the transport itself: a Reticulum announce (signed with Ed25519) can carry
the node's FIPS npub, and a FIPS flow to that npub is authenticated by FIPS's
Noise handshakes. Nothing needs to be cross-signed for routing to be safe.

**Reticulum is pluggable, FIPS is not.** Reticulum loads custom interface
classes at runtime, so Reticulum-over-FIPS is a pure add-on. FIPS instantiates
only its built-in transports, so FIPS-over-Reticulum is done by presenting the
tunnel to FIPS as a localhost UDP peer. A native FIPS transport could be added
upstream later without changing the wire envelope.

**Frame sizes point in one direction.** A Reticulum frame (500 bytes) fits in a
FIPS datagram (1170 bytes of payload at the default MTU) with room to spare, so
Reticulum links across a FIPS hop get a larger MTU than usual. A FIPS frame can
be 1280 bytes, and the bloom-filter announce is 1035 bytes and never split, so
FIPS-over-Reticulum must fragment in the bridge whenever the Reticulum link MDU
is below that, which is the case on every 500-byte hop. Reticulum paths that are
all IP-class (Backbone/TCP interfaces, link MTU discovery) can carry FIPS frames
whole.

**FIPS's control plane is sized for IP links.** Ten-second heartbeats, 30-second
link-dead timeouts, kilobyte bloom announces and two-minute rekeys are fine
over an internet-class Reticulum path and unrealistic over LoRa. FIPS's own
design documents list radio (51-222 byte MTU) as a future direction. The bridge
exposes FIPS-over-Reticulum for backbone-class Reticulum paths and treats
LoRa-class paths as experimental.

**FIPS flows have no far-end close and no reliability.** A peer that
disappears is invisible to the native API. The bridge runs its own HELLO/PING/
PONG liveness on every flow and tears down or redials accordingly.

**One FIPS MTU per node.** FIPS takes the minimum MTU over all its transports
and applies it to the whole node, including its IPv6 interface. The tunnel keeps
the local UDP shim at 1280 bytes and fragments inside the bridge, so running a
tunnel never shrinks a FIPS node's IPv6 MTU.

**Licensing and community stance.** The Reticulum maintainer has made clear
that the reference implementation is licensed, that machine-generated
reimplementations are unwelcome, and that the protocol itself is public domain.
This project therefore imports `rns` as a dependency and copies nothing from
it. FIPS is MIT; its Python test client for the native API documents the line
protocol, which this project reimplements independently in `rnsfips.fipsapi`.

## Where the bridge sits

```
Reticulum apps (LXMF, rncp, ...)          IPv6 apps / FIPS native apps
        |                                           |
   Reticulum Transport                      FIPS FSP + FMP
        |                                           |
   FIPSInterface  ---- FIPS native API ---->  FIPS daemon      (Reticulum over FIPS)
        |
   Reticulum Link  <--- rnsfips tunnel  <---  FIPS daemon (UDP 127.0.0.1)   (FIPS over Reticulum)
```

Both directions keep each stack's end-to-end encryption intact. Traffic on the
bridged hop is encrypted twice (by the carried stack end to end, and by the
carrying stack hop by hop or per link).
