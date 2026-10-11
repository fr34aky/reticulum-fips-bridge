"""FIPS over Reticulum: carry FIPS wire frames across Reticulum links.

One ``Tunnel`` is a Reticulum endpoint (destination ``rnsfips.tunnel``) that
keeps one Reticulum ``Link`` per remote tunnel. For each link it binds a local
UDP socket and asks the local FIPS daemon, over its control socket, to peer
with the remote node's npub at that address. FIPS then runs its own Noise
handshakes and routing through the link, unaware of Reticulum.

* FIPS -> link: datagrams arriving on the UDP socket go out as unproven
  ``RNS.Packet``s on the link, fragmented by the bridge when larger than the
  link MDU.
* link -> FIPS: ``FIPS_FRAME``/``FIPS_FRAG`` packets are reassembled and
  written to the FIPS daemon's UDP transport address.

Peers are configured by destination hash. Inbound links must identify
themselves (``Link.identify``) and are admitted by the ``allow`` list.
"""

from __future__ import annotations

import configparser
import os
import selectors
import signal
import socket
import threading
import time

import RNS
from RNS.vendor import umsgpack

from . import __version__
from . import envelope as env
from .fipsctl import FipsControl, FipsControlError

APP_NAME = "rnsfips"
ASPECT = "tunnel"
IDENTITY_FILE = "rnsfips_tunnel"


class TunnelConfig:
    def __init__(self, **kw):
        self.fips_udp_addr = kw.get("fips_udp_addr", "127.0.0.1:2121")
        self.fips_control = kw.get("fips_control") or None
        self.bind_addr = kw.get("bind_addr", "127.0.0.1")
        self.announce_interval = float(kw.get("announce_interval", 1800))
        self.peers = [bytes.fromhex(h) for h in _split(kw.get("peers", ""))]
        allow = _split(kw.get("allow", ""))
        self.allow_any = "any" in allow
        self.allow = {bytes.fromhex(h) for h in allow if h != "any"}
        self.auto_peer = _as_bool(kw.get("auto_peer", "no"))
        self.reconnect_wait = float(kw.get("reconnect_wait", 5))
        self.max_reconnect_wait = float(kw.get("max_reconnect_wait", 120))
        self.connect_refresh = float(kw.get("connect_refresh", 30))
        self.identity_path = kw.get("identity_path") or None
        self.npub = kw.get("npub") or None  # normally read from the FIPS daemon
        self.mdu_override = int(kw.get("mdu_override", 0) or 0)
        self.frag_timeout = float(kw.get("frag_timeout", 5))
        self.control_socket = kw.get("control_socket") or default_control_socket()
        # The tunnel is a client of rnsd's shared instance. Only with
        # standalone = yes does it run its own Reticulum instance (a host
        # without rnsd); otherwise it waits for rnsd rather than racing it.
        self.standalone = _as_bool(kw.get("standalone", "no"))
        self.instance_name = kw.get("instance_name", "default")

    @classmethod
    def from_file(cls, path: str) -> "TunnelConfig":
        cp = configparser.ConfigParser()
        with open(os.path.expanduser(path)) as f:
            cp.read_file(f)
        section = cp["tunnel"] if cp.has_section("tunnel") else cp["DEFAULT"]
        return cls(**dict(section))


def default_control_socket() -> str:
    """Short path for the tunnel's own control socket (Unix socket paths are length-limited)."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "rnsfips", "tunnel.sock")
    return f"/tmp/rnsfips-tunnel-{os.getuid()}.sock"


def _split(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value).replace(",", " ").split() if v.strip()]


def _as_bool(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _parse_addr(spec: str) -> tuple[str, int]:
    host, port = spec.rsplit(":", 1)
    return host.strip("[]"), int(port)


class Peer:
    """One remote tunnel: a Reticulum link plus a local UDP socket toward FIPS."""

    def __init__(self, tunnel: "Tunnel", dest_hash: bytes | None, initiator: bool):
        self.tunnel = tunnel
        self.dest_hash = dest_hash
        self.initiator = initiator
        self.link: RNS.Link | None = None
        self.identity_hash: bytes | None = None
        self.npub: str | None = None
        self.remote_nonce = None
        self.sock: socket.socket | None = None
        self.udp_port: int | None = None
        self.fips_addr: tuple[str, int] = tunnel.fips_udp_addr
        self.fragmenter = env.Fragmenter()
        self.reassembler = env.Reassembler(timeout=tunnel.config.frag_timeout)
        self.pending_hello: bytes | None = None
        self.identified = False
        self.last_connect = 0.0
        self.rx_frames = 0
        self.tx_frames = 0
        self.tx_drops = 0
        self.up_since = 0.0

    @property
    def label(self) -> str:
        if self.dest_hash:
            return RNS.prettyhexrep(self.dest_hash)
        return f"inbound link {RNS.prettyhexrep(self.link.link_id) if self.link else '?'}"

    @property
    def up(self) -> bool:
        return self.link is not None and self.link.status == RNS.Link.ACTIVE and self.sock is not None

    # ---- link side ------------------------------------------------------------

    def send_hello(self):
        hello = env.encode_hello({
            "impl": "rnsfips", "ver": __version__, "nonce": self.tunnel.nonce,
            "npub": self.tunnel.npub, "tid": None,
        })
        RNS.Packet(self.link, hello, create_receipt=False).send()

    def on_packet(self, data: bytes, packet):
        try:
            kind, payload = env.split(data)
        except env.EnvelopeError:
            return
        if kind == env.HELLO:
            if not self.identified:
                self.pending_hello = payload
                return
            self._on_hello(payload)
        elif kind == env.FIPS_FRAME:
            self._to_fips(payload)
        elif kind == env.FIPS_FRAG:
            try:
                frame = self.reassembler.feed(payload)
            except env.EnvelopeError:
                return
            if frame is not None:
                self._to_fips(frame)
        elif kind == env.PING:
            RNS.Packet(self.link, env.pong(), create_receipt=False).send()

    def _on_hello(self, payload: bytes):
        try:
            hello = env.decode_hello(payload)
        except env.EnvelopeError as e:
            RNS.log(f"tunnel {self.label}: bad HELLO ({e}); closing", RNS.LOG_WARNING)
            self.close()
            return
        npub = hello.get("npub")
        if not isinstance(npub, str) or not npub.startswith("npub1"):
            RNS.log(f"tunnel {self.label}: HELLO without npub; closing", RNS.LOG_WARNING)
            self.close()
            return
        restarted = self.remote_nonce is not None and hello.get("nonce") != self.remote_nonce
        self.remote_nonce = hello.get("nonce")
        self.npub = npub
        if self.sock is None:
            self.tunnel.open_shim(self)
        self.up_since = time.time()
        RNS.log(f"tunnel {self.label} up: FIPS peer {npub} via UDP {self.tunnel.config.bind_addr}:{self.udp_port}"
                f"{' (peer restarted)' if restarted else ''}", RNS.LOG_NOTICE)
        self.tunnel.fips_connect(self, force=True)

    def _to_fips(self, frame: bytes):
        if self.sock is None:
            return
        self.rx_frames += 1
        try:
            self.sock.sendto(frame, self.fips_addr)
        except OSError as e:
            RNS.log(f"tunnel {self.label}: cannot write to FIPS at {self.fips_addr}: {e}", RNS.LOG_DEBUG)

    # ---- FIPS side --------------------------------------------------------------

    def from_fips(self, frame: bytes, source: tuple[str, int]):
        self.fips_addr = source
        link = self.link
        if link is None or link.status != RNS.Link.ACTIVE:
            self.tx_drops += 1
            return
        mdu = self.tunnel.config.mdu_override or link.get_mdu()
        try:
            parts = self.fragmenter.encode(frame, mdu)
        except env.EnvelopeError as e:
            self.tx_drops += 1
            RNS.log(f"tunnel {self.label}: dropping {len(frame)}-byte frame: {e}", RNS.LOG_DEBUG)
            return
        for part in parts:
            RNS.Packet(link, part, create_receipt=False).send()
        self.tx_frames += 1

    def close(self):
        link, self.link = self.link, None
        if link is not None and link.status != RNS.Link.CLOSED:
            try:
                link.teardown()
            except Exception:
                pass
        self.tunnel.close_shim(self)


class Tunnel:
    def __init__(self, config: TunnelConfig, reticulum=None):
        self.config = config
        self.reticulum = reticulum
        self.nonce = os.urandom(8)
        self.fips_udp_addr = _parse_addr(config.fips_udp_addr)
        self.ctl = FipsControl(config.fips_control)
        self.npub = config.npub
        self.lock = threading.RLock()
        self.peers: dict[bytes, Peer] = {}       # initiator side, by dest hash
        self.inbound: dict[bytes, Peer] = {}     # responder side, by link id
        self._by_sock: dict[int, Peer] = {}
        self._backoff: dict[bytes, tuple[float, float]] = {}
        self._discovered: dict[bytes, dict] = {}
        self._stop = threading.Event()
        self._sel = selectors.DefaultSelector()
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._sel.register(self._wake_r, selectors.EVENT_READ, None)
        self._threads: list[threading.Thread] = []
        self.identity: RNS.Identity | None = None
        self.destination: RNS.Destination | None = None
        self.last_announce = 0.0

    # ---- lifecycle --------------------------------------------------------------

    def start(self):
        if self.reticulum is None:
            self.reticulum = RNS.Reticulum()
        if self.npub is None:
            self.npub = self._read_npub()
        self.identity = self._load_identity()
        self.destination = RNS.Destination(self.identity, RNS.Destination.IN, RNS.Destination.SINGLE, APP_NAME, ASPECT)
        self.destination.set_proof_strategy(RNS.Destination.PROVE_NONE)
        self.destination.set_link_established_callback(self._on_inbound_link)
        RNS.Transport.register_announce_handler(_AnnounceHandler(self))
        RNS.log(f"rnsfips tunnel {RNS.prettyhexrep(self.destination.hash)} for FIPS node {self.npub}", RNS.LOG_NOTICE)
        self.announce()
        for h in self.config.peers:
            self.peers[h] = Peer(self, h, initiator=True)
        t1 = threading.Thread(target=self._io_loop, name="rnsfips-tunnel-io", daemon=True)
        t2 = threading.Thread(target=self._supervisor, name="rnsfips-tunnel-sup", daemon=True)
        self._threads = [t1, t2]
        t1.start()
        t2.start()
        self._control = None
        if str(self.config.control_socket).lower() not in ("off", "no", "false", ""):
            try:
                self._control = _ControlServer(self, self.config.control_socket)
                self._control.start()
                self._threads.append(self._control)
            except OSError as e:
                RNS.log(f"rnsfips tunnel: cannot open control socket {self.config.control_socket}: {e}", RNS.LOG_WARNING)
        return self

    def stop(self):
        self._stop.set()
        self._wake()
        if getattr(self, "_control", None) is not None:
            self._control.close()
        with self.lock:
            for p in list(self.peers.values()) + list(self.inbound.values()):
                p.close()
        for t in self._threads:
            t.join(timeout=3)

    def _read_npub(self) -> str:
        delay = 1.0
        while not self._stop.is_set():
            try:
                return self.ctl.show_status()["npub"]
            except (FipsControlError, KeyError) as e:
                RNS.log(f"rnsfips tunnel: waiting for FIPS control socket {self.ctl.socket_path} ({e})", RNS.LOG_WARNING)
                self._stop.wait(delay)
                delay = min(delay * 2, 30)
        raise SystemExit("stopped before FIPS was reachable")

    def _load_identity(self) -> RNS.Identity:
        path = self.config.identity_path or os.path.join(RNS.Reticulum.identitypath, IDENTITY_FILE)
        if os.path.isfile(path):
            identity = RNS.Identity.from_file(path)
            if identity is not None:
                return identity
            RNS.log(f"rnsfips tunnel: could not load identity {path}; creating a new one", RNS.LOG_WARNING)
        identity = RNS.Identity()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        identity.to_file(path)
        return identity

    def announce(self):
        app_data = umsgpack.packb({"v": env.VERSION, "npub": self.npub, "impl": "rnsfips", "ver": __version__})
        self.destination.announce(app_data=app_data)
        self.last_announce = time.time()

    # ---- inbound links ------------------------------------------------------------

    def _on_inbound_link(self, link: RNS.Link):
        peer = Peer(self, None, initiator=False)
        peer.link = link
        with self.lock:
            self.inbound[link.link_id] = peer
        link.set_packet_callback(peer.on_packet)
        link.set_link_closed_callback(lambda l: self._on_closed(peer))
        link.set_remote_identified_callback(lambda l, identity: self._on_identified(peer, identity))
        RNS.log(f"tunnel: inbound link {RNS.prettyhexrep(link.link_id)}; awaiting identification", RNS.LOG_DEBUG)
        # No HELLO yet: the initiator is admitted only after it identifies, and a
        # HELLO sent before that would make a refused initiator believe the tunnel
        # is up and tell its FIPS daemon to dial.

    def _on_identified(self, peer: Peer, identity: RNS.Identity):
        dest_hash = RNS.Destination.hash(identity, APP_NAME, ASPECT)
        peer.identity_hash = identity.hash
        peer.dest_hash = dest_hash
        if not (self.config.allow_any or dest_hash in self.config.allow or dest_hash in self.config.peers):
            RNS.log(f"tunnel: refusing inbound link from {RNS.prettyhexrep(dest_hash)} (not in allow list)", RNS.LOG_WARNING)
            peer.close()
            return
        peer.identified = True
        peer.send_hello()
        if peer.pending_hello is not None:
            hello, peer.pending_hello = peer.pending_hello, None
            peer._on_hello(hello)

    # ---- outbound links -------------------------------------------------------------

    def _supervisor(self):
        while not self._stop.is_set():
            try:
                self._supervise_once()
            except Exception as e:
                RNS.log(f"rnsfips tunnel supervisor error: {e}", RNS.LOG_ERROR)
            self._stop.wait(1.0)

    def _supervise_once(self):
        now = time.time()
        if now - self.last_announce > self.config.announce_interval:
            self.announce()
        with self.lock:
            peers = list(self.peers.values())
        for peer in peers:
            if peer.link is not None and peer.link.status in (RNS.Link.ACTIVE, RNS.Link.PENDING, RNS.Link.HANDSHAKE):
                if peer.up and now - peer.last_connect > self.config.connect_refresh:
                    self.fips_connect(peer)
                continue
            next_try, _ = self._backoff.get(peer.dest_hash, (0, 0))
            if now < next_try:
                continue
            self._dial(peer)

    def _dial(self, peer: Peer):
        h = peer.dest_hash
        if not RNS.Transport.has_path(h):
            RNS.Transport.request_path(h)
            self._schedule_retry(h)
            return
        identity = RNS.Identity.recall(h)
        if identity is None:
            self._schedule_retry(h)
            return
        dest = RNS.Destination(identity, RNS.Destination.OUT, RNS.Destination.SINGLE, APP_NAME, ASPECT)
        link = RNS.Link(dest, established_callback=lambda l: self._on_established(peer, l),
                        closed_callback=lambda l: self._on_closed(peer))
        peer.link = link
        peer.identified = True
        link.set_packet_callback(peer.on_packet)
        RNS.log(f"tunnel {peer.label}: link requested", RNS.LOG_DEBUG)

    def _on_established(self, peer: Peer, link: RNS.Link):
        if peer.link is not link:
            return
        link.identify(self.identity)
        peer.send_hello()
        self._backoff.pop(peer.dest_hash, None)
        RNS.log(f"tunnel {peer.label}: link established, MDU {link.get_mdu()}", RNS.LOG_DEBUG)

    def _on_closed(self, peer: Peer):
        RNS.log(f"tunnel {peer.label}: link closed", RNS.LOG_VERBOSE)
        peer.link = None
        self.close_shim(peer)
        if peer.initiator:
            self._schedule_retry(peer.dest_hash)
        else:
            with self.lock:
                for k, v in list(self.inbound.items()):
                    if v is peer:
                        del self.inbound[k]

    def _schedule_retry(self, dest_hash: bytes):
        _, wait = self._backoff.get(dest_hash, (0, 0))
        wait = min(max(self.config.reconnect_wait, wait * 2), self.config.max_reconnect_wait)
        self._backoff[dest_hash] = (time.time() + wait, wait)

    # ---- UDP shim toward FIPS ---------------------------------------------------------

    def open_shim(self, peer: Peer):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.config.bind_addr, 0))
        sock.setblocking(False)
        peer.sock = sock
        peer.udp_port = sock.getsockname()[1]
        with self.lock:
            self._by_sock[sock.fileno()] = peer
        self._sel.register(sock, selectors.EVENT_READ, peer)
        self._wake()

    def close_shim(self, peer: Peer):
        sock, peer.sock = peer.sock, None
        if sock is None:
            return
        try:
            self._sel.unregister(sock)
        except (KeyError, ValueError, OSError):
            pass
        with self.lock:
            self._by_sock.pop(sock.fileno(), None)
        sock.close()
        if peer.npub:
            try:
                self.ctl.disconnect(peer.npub)
            except FipsControlError as e:
                RNS.log(f"tunnel {peer.label}: FIPS disconnect failed: {e}", RNS.LOG_DEBUG)
        peer.udp_port = None

    def fips_connect(self, peer: Peer, force: bool = False):
        if peer.npub is None or peer.udp_port is None:
            return
        try:
            r = self.ctl.connect(peer.npub, f"{self.config.bind_addr}:{peer.udp_port}", "udp")
            peer.last_connect = time.time()
            if force:
                RNS.log(f"tunnel {peer.label}: FIPS connect -> {r}", RNS.LOG_DEBUG)
        except FipsControlError as e:
            peer.last_connect = time.time()
            RNS.log(f"tunnel {peer.label}: FIPS connect failed: {e}", RNS.LOG_WARNING)

    def _wake(self):
        try:
            self._wake_w.send(b"x")
        except OSError:
            pass

    def _io_loop(self):
        while not self._stop.is_set():
            try:
                events = self._sel.select(timeout=1.0)
            except OSError:
                continue
            for key, _ in events:
                if key.data is None:
                    try:
                        self._wake_r.recv(4096)
                    except OSError:
                        pass
                    continue
                peer: Peer = key.data
                sock = peer.sock
                if sock is None:
                    continue
                for _ in range(64):
                    try:
                        frame, source = sock.recvfrom(65535)
                    except BlockingIOError:
                        break
                    except OSError:
                        break
                    peer.from_fips(frame, source)
            for peer in list(self._by_sock.values()):
                peer.reassembler.expire()

    # ---- control commands ----------------------------------------------------------

    def add_peer(self, dest_hash: bytes) -> bool:
        with self.lock:
            if dest_hash in self.peers or dest_hash == self.destination.hash:
                return False
            self.peers[dest_hash] = Peer(self, dest_hash, initiator=True)
        return True

    def remove_peer(self, dest_hash: bytes) -> bool:
        with self.lock:
            peer = self.peers.pop(dest_hash, None)
        if peer is None:
            return False
        peer.initiator = False  # no reconnect from the close callback
        peer.close()
        self._backoff.pop(dest_hash, None)
        return True

    def control_command(self, command: str, params: dict) -> dict:
        if command == "status":
            return self.status()
        if command == "announce":
            self.announce()
            return {"announced": True}
        if command in ("add_peer", "remove_peer"):
            try:
                dest = bytes.fromhex(params["dest"])
            except (KeyError, ValueError, TypeError):
                raise ValueError("params.dest must be a 32-hex-digit destination hash")
            if len(dest) != 16:
                raise ValueError("params.dest must be a 16-byte destination hash")
            changed = self.add_peer(dest) if command == "add_peer" else self.remove_peer(dest)
            return {"changed": changed}
        raise ValueError(f"unknown command {command!r}")

    # ---- status ---------------------------------------------------------------------

    def status(self) -> dict:
        with self.lock:
            peers = list(self.peers.values()) + list(self.inbound.values())
        return {
            "destination": self.destination.hash.hex() if self.destination else None,
            "npub": self.npub,
            "peers": [{
                "dest": p.dest_hash.hex() if p.dest_hash else None, "initiator": p.initiator, "up": p.up,
                "npub": p.npub, "udp_port": p.udp_port, "rx_frames": p.rx_frames, "tx_frames": p.tx_frames,
                "tx_drops": p.tx_drops, "mdu": (p.link.get_mdu() if p.link and p.link.status == RNS.Link.ACTIVE else None),
            } for p in peers],
            "discovered": {h.hex(): d for h, d in self._discovered.items()},
        }


class _ControlServer(threading.Thread):
    """JSON-line control socket, same shape as the FIPS control socket: one request per connection."""

    def __init__(self, tunnel: Tunnel, path: str):
        super().__init__(name="rnsfips-tunnel-ctl", daemon=True)
        self.tunnel = tunnel
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            if control_socket_alive(path):
                raise RuntimeError(f"another rnsfips tunnel is already serving {path}")
            os.unlink(path)  # stale: left by a tunnel that died without cleanup
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        os.chmod(path, 0o660)
        self.sock.listen(8)
        self.sock.settimeout(0.5)

    def run(self):
        while not self.tunnel._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket):
        try:
            conn.settimeout(5)
            buf = b""
            while b"\n" not in buf and len(buf) < 4096:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            import json

            try:
                req = json.loads(buf.split(b"\n", 1)[0].decode("utf-8") or "{}")
                data = self.tunnel.control_command(req.get("command", ""), req.get("params") or {})
                reply = {"status": "ok", "data": _jsonable(data)}
            except Exception as e:
                reply = {"status": "error", "message": f"{type(e).__name__}: {e}"}
            conn.sendall(json.dumps(reply).encode("utf-8") + b"\n")
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass


def _jsonable(value):
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(k.hex() if isinstance(k, bytes) else k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def control_socket_alive(path: str, timeout: float = 1.0) -> bool:
    """True when a process is listening on ``path``; False for a missing or stale socket file."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


def control_query(command: str, params: dict | None = None, socket_path: str | None = None, timeout: float = 5.0) -> dict:
    """Client side of the tunnel control socket."""
    import json

    path = socket_path or default_control_socket()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall(json.dumps({"command": command, "params": params or {}}).encode("utf-8") + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    reply = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    if reply.get("status") != "ok":
        raise RuntimeError(reply.get("message", "unknown error"))
    return reply.get("data") or {}


class _AnnounceHandler:
    def __init__(self, tunnel: Tunnel):
        self.tunnel = tunnel
        self.aspect_filter = f"{APP_NAME}.{ASPECT}"
        self.receive_path_responses = True

    def received_announce(self, destination_hash, announced_identity, app_data):
        RNS.log(f"tunnel: announce from {RNS.prettyhexrep(destination_hash)} app_data={len(app_data) if app_data else 0}B", RNS.LOG_DEBUG)
        info = None
        if app_data:
            try:
                info = umsgpack.unpackb(app_data)
            except Exception:
                info = None
        if not isinstance(info, dict):
            return
        self.tunnel._discovered[destination_hash] = {"npub": info.get("npub"), "impl": info.get("impl"), "ver": info.get("ver"), "seen": time.time()}
        cfg = self.tunnel.config
        if cfg.auto_peer and destination_hash not in self.tunnel.peers and destination_hash != self.tunnel.destination.hash \
                and (cfg.allow_any or destination_hash in cfg.allow):
            RNS.log(f"tunnel: auto-peering with discovered tunnel {RNS.prettyhexrep(destination_hash)} ({info.get('npub')})", RNS.LOG_NOTICE)
            with self.tunnel.lock:
                self.tunnel.peers[destination_hash] = Peer(self.tunnel, destination_hash, initiator=True)


def main(args) -> int:
    path = os.path.expanduser(args.config)
    if not os.path.exists(path):
        print(f"no tunnel configuration at {path}; example:\n", file=os.sys.stderr)
        print(EXAMPLE_CONFIG, file=os.sys.stderr)
        return 2
    config = TunnelConfig.from_file(path)
    loglevel = min(RNS.LOG_NOTICE + args.verbose, RNS.LOG_EXTREME)
    if config.standalone:
        reticulum = RNS.Reticulum(configdir=args.rns_configdir, loglevel=loglevel)
    else:
        from .rnsutil import wait_for_shared_instance

        wait_for_shared_instance(config.instance_name, on_wait=lambda: print(
            "rnsfips tunnel: waiting for the rnsd shared instance (set standalone = yes to run without rnsd)",
            file=os.sys.stderr, flush=True))
        try:
            reticulum = RNS.Reticulum(configdir=args.rns_configdir, loglevel=loglevel, require_shared_instance=True)
        except SystemError as e:
            # rnsd went away between the probe and the start; let the service manager retry.
            print(f"rnsfips tunnel: {e}", file=os.sys.stderr)
            return 3
    # Logs go through RNS.log to stdout; keep them visible when redirected to a file.
    try:
        os.sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    try:
        tunnel = Tunnel(config, reticulum).start()
    except RuntimeError as e:
        print(f"rnsfips tunnel: {e}", file=os.sys.stderr)
        return 4
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    # A plain exit would leave the control socket file behind and make
    # `rnsfips status` report "connection refused" instead of "not running".
    stop.wait()
    tunnel.stop()
    return 0


EXAMPLE_CONFIG = """\
[tunnel]
# Address of the local FIPS daemon's UDP transport
fips_udp_addr = 127.0.0.1:2121
# Remote tunnels to link to (destination hashes shown in their logs)
peers =
# Which remote tunnels may link to us: destination hashes, or "any"
allow = any
# Also dial tunnels learned from announces, if allow admits them
# auto_peer = no
# Re-announce interval in seconds
announce_interval = 1800
# Control socket for `rnsfips status` / `rnsfips tunnel-ctl` ("off" to disable)
# control_socket = $XDG_RUNTIME_DIR/rnsfips/tunnel.sock
# Run an own Reticulum instance instead of joining rnsd's (hosts without rnsd)
# standalone = no
"""
