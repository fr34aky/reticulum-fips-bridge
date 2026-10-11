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
  peer. A FIPS node whose only way out is a Reticulum network still joins the
  FIPS mesh.

Reticulum is used purely as a library (the `rns` package); no Reticulum code
is copied or ported. FIPS is driven through its documented Unix sockets; no
FIPS code is modified.

See [docs/comparison.md](docs/comparison.md) for how the two stacks compare and
why the bridge sits where it does, [docs/design.md](docs/design.md) for the
wire envelope, MTU handling, lifecycle, discovery and why the bridge cannot
create routing loops, and [docs/status.md](docs/status.md) for the live
verification log.

## Status

Version 0.1.x. Both directions are implemented, covered by tests against a
fake FIPS daemon and a Docker lab with a real three-node FIPS mesh, and
verified live between three real nodes on the public FIPS mesh, including a
FIPS node reachable only through the tunnel (see
[docs/status.md](docs/status.md)). Bridges discover each other over
Reticulum announces, run as systemd user services, and report through
`rnsfips status`.

The FIPS native datagram API that the Reticulum-over-FIPS direction relies on
is itself experimental and off by default in FIPS; its surface may change.

## Which setup do you need?

The two directions are independent. Pick the one that matches your goal; a
node can run both, but nothing requires it.

| You want | Direction | What runs where |
| --- | --- | --- |
| Reticulum nodes at two or more sites to reach each other, with the public FIPS mesh as the backbone | **A: Reticulum over FIPS** | at each site: a FIPS daemon with the native API, `rnsd` with the `[[FIPS]]` interface |
| A FIPS node that has no internet path to the FIPS mesh (behind LoRa, serial, or a private network) to join the mesh through a Reticulum link | **B: FIPS over Reticulum** | on the cut-off node and on one node that is on the mesh: `rnsd` with a Reticulum link between them, plus `rnsfips tunnel` |

Rules of thumb:

* Direction A needs the FIPS native API enabled; direction B does not.
* Direction B needs a Reticulum path between the two `rnsd`s that does not
  depend on FIPS (a TCP interface, LoRa, anything). It also only does useful
  work for a FIPS node that cannot reach the mesh otherwise: FIPS never dials
  a peer it already holds a session with, so a tunnel between two nodes that
  peer directly sits idle.
* "Both quick starts" is never required. Follow one of the two walkthroughs
  below, after the common installation.

## Installation (every node, both directions)

`rnsd` must run in the same Python environment as `rnsfips`, because Reticulum
imports the interface inside its own interpreter. With pipx:

```sh
git clone https://github.com/fr34aky/reticulum-fips-bridge
pipx install ./reticulum-fips-bridge    # not on PyPI yet; one package per pipx install
pipx inject rnsfips rns --include-apps  # rnsd, rnstatus, rncp... from the same venv
readlink -f "$(command -v rnsd)"        # must point into .../pipx/venvs/rnsfips/
```

If a separate `rns` pipx venv already exists, `pipx uninstall rns` so its
`rnsd` does not shadow the one that can import the bridge. Plain `pip install
rns ./reticulum-fips-bridge` into one venv works just as well.

Then create the Reticulum config if it does not exist yet and turn the node
into a transport node:

```sh
rnsd --exampleconfig > ~/.reticulum/config   # only if ~/.reticulum/config is missing
```

In `~/.reticulum/config`, set `enable_transport = yes` in the `[reticulum]`
section. Install the services now or later (see
[Running as services](#running-as-services)); the walkthroughs assume `rnsd`
runs as the user unit.

Requirements: Python 3.9+, `rns >= 1.5.5`, a FIPS daemon 0.5.0 or newer whose
control socket the user can open (group `fips` on a packaged install). Direction
A additionally needs the native API (Linux or FreeBSD; it uses
`SOCK_SEQPACKET` and `SCM_RIGHTS`). FIPS builds before commit 55fb970 cannot
complete discovery for a native flow to a node more than one hop away unless
the node's key is already cached; the interface works around this by
resolving `<npub>.fips` at the daemon's DNS responder before dialing, so leave
`dns.enabled` on.

## A: Reticulum over FIPS, step by step

Goal: `rnsd` at site 1 and `rnsd` at site 2 become one Reticulum network over
the FIPS mesh. Repeat steps 1 to 4 at every site.

**1. Enable the FIPS native API.** In `/etc/fips/fips.yaml`:

```yaml
node:
  native_api:
    enabled: true
```

then `sudo systemctl restart fips`. `fipsctl show status` prints the node's
npub; note it, the other sites need it.

**2. Install the interface shim** into Reticulum's interface directory:

```sh
rnsfips install-interface          # writes ~/.reticulum/interfaces/FIPSInterface.py
```

**3. Add the interface** to `~/.reticulum/config`, inside the `[interfaces]`
section, listing the other sites' npubs. The first site can leave `peers`
empty and let the others dial it:

```ini
[interfaces]
  [[FIPS]]
    type = FIPSInterface
    enabled = yes
    mode = full
    listen_port = 21075
    peers = npub1aaaa..., npub1bbbb...
    auto_peer = yes
```

`auto_peer = yes` makes the site dial every bridge it learns about from
announces and accept their flows, so beyond the first `peers` entry no site
needs the full list. Leave it off, and use `peers`/`allow`, on a bridge that
should only talk to an explicit set. Do not comment out individual keys of a
block you want active: an interface with its `peers` line commented out starts
with "0 configured peer(s)" and refuses the other side.

**4. Start rnsd** (`systemctl --user restart rnsd`) and check:

```sh
rnsfips status
```

The FIPS section must show a native API listener on 21075, and after the HELLO
exchange (a few seconds, up to about 30 s when FIPS has to discover the peer)
the Reticulum section shows one `FIPSPeerInterface[...]` per peer as `up`
with MTU 1169. `rnstatus` shows the same interfaces. From then on it is plain
Reticulum: announces from the far site arrive, `rnpath` finds destinations one
hop away via the FIPS peer interface, `rncp` and links work with an MTU of
1169 instead of 500.

**5. Add a third site** by repeating 1 to 4 with a single existing bridge in
`peers`. It learns the others from that bridge's announce within a minute and,
with `auto_peer`, dials them directly.

Interface options:

| key | default | meaning |
| --- | --- | --- |
| `fips_socket` | auto | path to the FIPS `api.sock` |
| `listen_port` | 21075 | FIPS application port this interface holds and dials (1024-65535) |
| `remote_port` | `listen_port` | port to dial on peers that do not specify one |
| `peers` | | comma-separated `npub[:port]` to dial and keep connected |
| `allow` | | extra npubs accepted inbound (configured peers are always accepted) |
| `accept_unknown` | no | accept inbound flows from any npub |
| `auto_peer` | no | dial bridges learned from announces, and accept their inbound flows |
| `announce` | yes | announce this bridge over Reticulum (`rnsfips.bridge` destination) so other bridges can discover it |
| `announce_interval` | 3600 s | re-announce period; a fresh announce also goes out shortly after a new peer comes up |
| `ping_interval` / `peer_timeout` | 15 / 60 s | bridge-level liveness, since FIPS flows have no far-end close |
| `mtu` | computed | cap the interface MTU below what the FIPS flow allows |
| `fips_dns` | `[::1]:5354` | FIPS `.fips` resolver used to prime the daemon's identity cache before dialing; `off` to disable |

All standard Reticulum interface options (`mode`, `network_name`,
`passphrase`, `announce_cap`, `bitrate`, `outgoing`, ...) apply and are inherited
by every peer interface. Use `mode = full` on both ends for site-to-site;
`mode = gateway` on the side that should resolve paths for the other.

## B: FIPS over Reticulum, step by step

Goal: the FIPS daemon on node **X**, which cannot reach the FIPS mesh, joins it
through the FIPS daemon on node **Y**, which is on the mesh. Both nodes run
`rnsd`; the tunnel rides on a Reticulum link between them.

**1. Give the two rnsds a Reticulum path** that does not go through FIPS. For
two machines on the internet, a TCP interface is the simplest. On Y, in
`~/.reticulum/config` under `[interfaces]`:

```ini
  [[TCP Server]]
    type = TCPServerInterface
    enabled = yes
    listen_ip = 0.0.0.0
    listen_port = 4242
```

and open TCP 4242 in Y's firewall. On X:

```ini
  [[Y]]
    type = TCPClientInterface
    enabled = yes
    target_host = <Y's address>
    target_port = 4242
```

The client keys are `target_host` and `target_port`; the server keys
`listen_ip`/`listen_port` in a client block make rnsd crash at start. Restart
`rnsd` on both, and confirm with `rnstatus` on X that the TCP interface is
`Up`. Any other Reticulum link works the same way: LoRa, I2P, an existing
Reticulum network both are already on.

**2. Write the tunnel config** on both nodes, `~/.config/rnsfips/tunnel.conf`:

```ini
[tunnel]
# the local FIPS daemon's UDP transport
fips_udp_addr = 127.0.0.1:2121
# remote tunnel destination hashes to dial; may stay empty with auto_peer
peers =
# which remote tunnels may link to us: destination hashes, or any
allow = any
# dial every tunnel learned from announces that allow admits
auto_peer = yes
```

The tunnel needs the FIPS control socket and a UDP transport on the FIPS
daemon; the native API is not used.

**3. Start the tunnel as a service** on both nodes:

```sh
rnsfips install-units
systemctl --user daemon-reload
systemctl --user enable --now rnsfips-tunnel.service
```

`rnsfips tunnel -v` in a terminal does the same in the foreground for a first
look, but do not run it next to the service: both would want the same control
socket, and the second one exits with a message saying so.

**4. Connect the two tunnels.** Each tunnel announces itself over Reticulum
when it starts and every 30 minutes. If X's tunnel started before the
Reticulum path to Y existed, it has not heard Y yet; trigger an announce on
either side:

```sh
rnsfips tunnel-ctl announce
```

Within seconds `rnsfips status` on both nodes lists the other tunnel under
"discovered", and with `auto_peer` one of them dials. Without `auto_peer`, put
the other side's destination hash (the `destination` line of its
`rnsfips status`) into `peers` on one side and restart that tunnel. Either
way both sides then show the peer as `up` with an `npub=` and a `udp=` port,
and the FIPS section shows a "tunnel peer" at `127.0.0.1:<port>`. Listing
each other in `peers` on both sides is fine: only one link per pair is kept.

**5. Check FIPS.** On X, `fipsctl show peers` lists Y's npub with a loopback
transport address, and `fipsctl show status` reports the mesh size learned
through it. `fipsctl probe <npub of any mesh node>` from X now routes through
Y. All FIPS traffic from X, `ping6 <npub>.fips`, the native API, everything,
flows through Reticulum.

**6. Make X depend on the tunnel only** (optional, for a real test or a node
that must not have other transports). In X's `/etc/fips/fips.yaml` set
`peers: []`, disable `node.rendezvous.nostr` and `node.rendezvous.lan`, bind
the UDP transport to `127.0.0.1:2121`, and restart FIPS. X's only peer is then
Y via the tunnel; it reaches the rest of the mesh through FIPS's own spanning
tree and bloom routing.

Tunnel options (`[tunnel]` section):

| key | default | meaning |
| --- | --- | --- |
| `fips_udp_addr` | `127.0.0.1:2121` | the local FIPS daemon's UDP transport address |
| `fips_control` | auto | path to the FIPS control socket |
| `peers` | | destination hashes of remote tunnels to dial |
| `allow` | | hashes admitted inbound, or `any` |
| `auto_peer` | no | also dial tunnels learned from announces, if `allow` admits them |
| `announce_interval` | 1800 s | re-announce period |
| `control_socket` | `$XDG_RUNTIME_DIR/rnsfips/tunnel.sock` | control socket for `rnsfips status` and `tunnel-ctl`; `off` to disable |
| `standalone` | no | run an own Reticulum instance instead of joining rnsd's (hosts without rnsd) |
| `reconnect_wait` / `max_reconnect_wait` | 5 / 120 s | redial backoff |

FIPS frames larger than the Reticulum link MDU are fragmented by the bridge; on
500-byte Reticulum hops a FIPS bloom announce costs three packets, on TCP-class
hops it fits in one. FIPS's timers (10 s heartbeats, 30 s link-dead) suit
internet-class Reticulum paths; LoRa-class paths are experimental, see
[docs/design.md](docs/design.md#fips-over-reticulum).

## Checking on it

`rnsfips status` shows the three views side by side: the FIPS node (native API
listeners and flows, loopback tunnel peers, drop counters), the Reticulum FIPS
interfaces from the running `rnsd` plus discovered bridges, and the tunnel
(per-peer link state, MDU, frame counters, discovered tunnels). `--json` gives
the raw data.

`rnsfips tunnel-ctl status|announce|add_peer <hash>|remove_peer <hash>` talks
to the running tunnel over its control socket.

Commands:

| command | purpose |
| --- | --- |
| `rnsfips install-interface` | install the Reticulum interface shim into `~/.reticulum/interfaces/` |
| `rnsfips install-units` | write systemd user units for `rnsd` and the tunnel |
| `rnsfips status` | FIPS node, Reticulum FIPS interfaces, discovered bridges and tunnel state in one view |
| `rnsfips tunnel` | run the FIPS-over-Reticulum tunnel daemon in the foreground |
| `rnsfips tunnel-ctl status\|announce\|add_peer\|remove_peer` | control a running tunnel |

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

Things that bite:

* Stop any `rnsd` you started by hand **before** enabling the unit. An rnsd
  that starts while another instance is running attaches to it as a client and
  never takes over when that instance exits; the service then looks active but
  serves nothing (`rnsfips status` reports "shared instance not reachable").
  `systemctl --user restart rnsd` fixes it.
* The tunnel unit requires `rnsd.service`, so restarting rnsd restarts the
  tunnel too. A tunnel-only node (direction B, step 6) is off the mesh for the
  few seconds until the other side redials.
* Set `standalone = yes` in `tunnel.conf` only on a host that runs no rnsd.
* `sudo rnsfips install-units --system` writes system units to
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
