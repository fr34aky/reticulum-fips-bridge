# reticulum-fips-bridge

A bridge between the [Reticulum Network Stack](https://github.com/markqvist/reticulum)
and the [FIPS](https://github.com/jmcorgan/fips) mesh, in both directions:

* **Reticulum over FIPS.** `FIPSInterface` is a Reticulum interface that
  carries Reticulum frames across the FIPS mesh using FIPS's native datagram
  API. Two Reticulum sites anywhere on a FIPS mesh become one Reticulum
  network; FIPS does the NAT traversal, routing and hop-by-hop encryption,
  Reticulum keeps its own end-to-end cryptography untouched.
* **FIPS over Reticulum.** `rnsfips tunnel` carries FIPS wire frames over
  Reticulum links and presents the far end to the local FIPS daemon as a UDP
  peer. FIPS nodes separated by a Reticulum backbone become FIPS peers.

Reticulum is used purely as a library (the `rns` package); no Reticulum code
is copied or ported. FIPS is driven through its documented Unix sockets; no
FIPS code is modified.

See [docs/comparison.md](docs/comparison.md) for how the two stacks compare and
why the bridge sits where it does, [docs/design.md](docs/design.md) for the
wire envelope, MTU handling and lifecycle details, and
[docs/status.md](docs/status.md) for the live verification log.

## Status

Version 0.1.0. Both directions are implemented, covered by tests against a
fake FIPS daemon and a Docker lab with a real three-node FIPS mesh, and
verified live between two sites on the public FIPS mesh (see
[docs/status.md](docs/status.md)). Bridges discover each other over
Reticulum announces, run as systemd user services, and report through
`rnsfips status`.

The FIPS native datagram API that the Reticulum-over-FIPS direction relies on
is itself experimental and off by default in FIPS; its surface may change.

## Commands

| command | purpose |
| --- | --- |
| `rnsfips install-interface` | install the Reticulum interface shim into `~/.reticulum/interfaces/` |
| `rnsfips install-units` | write systemd user units for `rnsd` and the tunnel |
| `rnsfips status` | FIPS node, Reticulum FIPS interfaces, discovered bridges and tunnel state in one view |
| `rnsfips tunnel` | run the FIPS-over-Reticulum tunnel daemon |
| `rnsfips tunnel-ctl status\|announce\|add_peer\|remove_peer` | control a running tunnel |

## Requirements

* Python 3.9+, `rns >= 1.5.5`
* A FIPS daemon (0.5.0 or newer) with `node.native_api.enabled: true`
  (Linux or FreeBSD: the native API needs `SOCK_SEQPACKET` and `SCM_RIGHTS`).
  FIPS builds before commit 55fb970 cannot complete discovery for a native
  flow to a node more than one hop away unless the node's key is already in
  the identity cache; the interface works around this by resolving
  `<npub>.fips` at the daemon's DNS responder before dialing, so leave
  `dns.enabled` on (it is by default).
* The user running `rnsd` must be able to open the FIPS API socket
  (group `fips` on a packaged install)

## Reticulum over FIPS: quick start

On every site:

```sh
pip install rns rnsfips            # or: pip install -e . from this checkout
rnsfips install-interface          # writes ~/.reticulum/interfaces/FIPSInterface.py (shim from the package)
```

`rnsd` must run in the same Python environment as `rnsfips`, because Reticulum
imports the interface inside its own interpreter. With pipx, install once and
expose Reticulum's tools from that venv rather than installing `rns` separately:

```sh
pipx install rnsfips               # or: pipx install /path/to/this/checkout
pipx inject rnsfips rns --include-apps
readlink -f "$(command -v rnsd)"   # must point into .../pipx/venvs/rnsfips/
```

If `~/.reticulum/config` does not exist yet, run `rnsd` once to create it (or
`rnsd --exampleconfig > ~/.reticulum/config`) and set `enable_transport = yes`.

Enable the native API on the FIPS daemon (`/etc/fips/fips.yaml`) and restart it:

```yaml
node:
  native_api:
    enabled: true
```

Add the interface to `~/.reticulum/config`, listing the other sites' FIPS npubs:

```ini
[interfaces]
  [[FIPS]]
    type = FIPSInterface
    enabled = yes
    mode = full
    listen_port = 21075
    peers = npub1aaaa..., npub1bbbb...
```

Start `rnsd`. `rnstatus` lists one `FIPSPeerInterface` per peer once the HELLO
exchange completes; Reticulum links across the FIPS hop negotiate an MTU of
about 1150 bytes instead of 500.

Interface options:

| key | default | meaning |
| --- | --- | --- |
| `fips_socket` | auto | path to the FIPS `api.sock` |
| `listen_port` | 21075 | FIPS application port this interface holds and dials (1024-65535) |
| `remote_port` | `listen_port` | port to dial on peers that do not specify one |
| `peers` | | comma-separated `npub[:port]` to dial and keep connected |
| `allow` | | extra npubs accepted inbound (configured peers are always accepted) |
| `accept_unknown` | no | accept inbound flows from any npub |
| `ping_interval` / `peer_timeout` | 15 / 60 s | bridge-level liveness, since FIPS flows have no far-end close |
| `mtu` | computed | cap the interface MTU below what the FIPS flow allows |
| `fips_dns` | `[::1]:5354` | FIPS `.fips` resolver used to prime the daemon's identity cache before dialing; `off` to disable |
| `announce` | yes | announce this bridge over Reticulum (`rnsfips.bridge` destination) so other bridges can discover it |
| `announce_interval` | 3600 s | re-announce period; a fresh announce also goes out shortly after a new peer comes up |
| `auto_peer` | no | dial bridges learned from announces, and accept their inbound flows |

### Discovery

Every interface announces a small Reticulum destination carrying its FIPS npub,
its port, and the bridges it already knows. A new site therefore needs only
**one** existing bridge in `peers`; with `auto_peer = yes` it learns the rest
from that peer's announce and dials them directly over FIPS, and existing
sites learn the newcomer the same way. Discovery rides on Reticulum
connectivity, so it never replaces the first configured peer. Learned bridges
are listed by `rnsfips status`. Leave `auto_peer` off on bridges that should
only talk to an explicit `peers`/`allow` list.

All standard Reticulum interface options (`mode`, `network_name`,
`passphrase`, `announce_cap`, `bitrate`, `outgoing`, ...) apply and are inherited
by every peer interface. Use `mode = full` on both ends for site-to-site;
`mode = gateway` on the side that should resolve paths for the other.

## FIPS over Reticulum: quick start

On each FIPS node that should reach the other across a Reticulum network, with
`rnsd` already running and connected to that network, create
`~/.config/rnsfips/tunnel.conf`:

```ini
[tunnel]
# the local FIPS daemon's UDP transport
fips_udp_addr = 127.0.0.1:2121
# remote tunnel destination hashes; fill in after the first start
peers =
# which remote tunnels may link to us: destination hashes, or any
allow = any
```

and run `rnsfips tunnel -v`. The daemon logs its own destination hash
(`rnsfips tunnel <hash> for FIPS node npub1...`). Put the other side's hash
into `peers` on at least one side and restart; the two tunnels link, exchange
npubs, and each tells its FIPS daemon to peer with the other over a
`127.0.0.1` UDP port. `fipsctl show peers` then lists the remote npub with a
loopback transport address, and FIPS traffic (`ping6 <npub>.fips`, the native
API, everything) flows through Reticulum.

The tunnel needs the FIPS control socket (group `fips`) and a UDP transport on
the FIPS daemon. FIPS frames larger than the Reticulum link MDU are fragmented
by the bridge; on 500-byte Reticulum hops a FIPS bloom announce costs three
packets. FIPS's timers (10 s heartbeats, 30 s link-dead) suit internet-class
Reticulum paths; LoRa-class paths are experimental, see
[docs/design.md](docs/design.md#fips-over-reticulum).

## Checking on it

`rnsfips status` shows the three views side by side: the FIPS node (native API
listeners and flows, loopback tunnel peers, drop counters), the Reticulum FIPS
interfaces from the running `rnsd`, and the tunnel (per-peer link state, MDU,
frame counters, discovered tunnels). `--json` gives the raw data.

`rnsfips tunnel-ctl status|announce|add_peer <hash>|remove_peer <hash>` talks
to the running tunnel over its control socket
(`$XDG_RUNTIME_DIR/rnsfips/tunnel.sock`, set `control_socket` in `tunnel.conf`
to move or disable it).

## Running as services

`rnsfips install-units` writes systemd user units for `rnsd` (with the FIPS
interface) and for the tunnel, using the executables of the environment it runs
from. Reference copies are in [systemd/](systemd/).

```sh
rnsfips install-units                 # ~/.config/systemd/user/{rnsd,rnsfips-tunnel}.service
systemctl --user daemon-reload
systemctl --user enable --now rnsd.service
systemctl --user enable --now rnsfips-tunnel.service   # only where ~/.config/rnsfips/tunnel.conf exists
loginctl enable-linger "$USER"        # keep user services running without a login session
```

The tunnel unit requires `rnsd.service` and waits for its shared instance to
answer before starting, because a tunnel started first would become its own
Reticulum instance.

Stop any `rnsd` you started by hand **before** enabling the unit. An rnsd that
starts while another instance is running attaches to it as a client and never
takes over when that instance exits; the service then looks active but serves
nothing (`rnsfips status` reports "shared instance not reachable"). If that
happens, `systemctl --user restart rnsd` fixes it. `sudo rnsfips install-units --system` writes system units to
`/etc/systemd/system` instead (tunnel config at `/etc/rnsfips/tunnel.conf`).

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                     # unit tests + multi-site Reticulum tests (~1 min)
lab/build.sh && lab/smoke.sh         # Docker: real FIPS mesh, three nodes
```

The test suite needs no FIPS daemon: `tests/fake_fips.py` implements the
native API line protocol over real Unix sockets, and Reticulum sites run as
subprocesses (`tests/site.py`). CI runs the suite on every push and pull
request (`.github/workflows/ci.yml`). Releases are tagged `vX.Y.Z`; see
[CHANGELOG.md](CHANGELOG.md).

The Docker lab copies the host's `fips`/`fipsctl` binaries into a Debian image
(no Rust toolchain needed) and brings up `site-a <-> relay <-> site-b`, where
the two sites share no IP network and reach each other only through FIPS.

## License

MIT. Reticulum is licensed under the Reticulum License; this project imports it
unmodified and copies none of its source.
