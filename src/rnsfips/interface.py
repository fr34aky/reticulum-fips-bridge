"""Reticulum interface that carries Reticulum frames over the FIPS mesh.

``FIPSInterface`` is the container Reticulum loads from its configuration.
It holds one FIPS native-API listener and dials the configured FIPS peers.
Every established flow becomes one ``FIPSPeerInterface``, a spawned child
registered with ``RNS.Transport``, the same arrangement Reticulum uses for
its own BackboneInterface and WeaveInterface.

Lifecycle, as Reticulum drives it:

* ``__init__(owner, configuration)`` is called by the loader. Reticulum then
  applies the standard options (``mode``, IFAC, announce settings, ``OUT``)
  to this object, registers it, and calls ``final_init``.
* ``final_init`` starts the threads. Children are only ever spawned from
  the I/O thread, after the peer's HELLO, so they copy settled values.
* ``detach`` stops everything. Reticulum's own detach path removes the
  children from Transport.

Threads:

* I/O thread: ``selectors`` over the listener, every flow, and a wakeup
  socket. All flow state changes happen here.
* Supervisor thread: the blocking setup calls (``listen``, ``connect``)
  against the daemon, with backoff; hands results to the I/O thread.
"""

from __future__ import annotations

import collections
import errno
import os
import selectors
import socket
import threading
import time

import RNS
from RNS.Interfaces.Interface import Interface

from . import __version__
from . import envelope as env
from .fipsapi import DEFAULT_PORT, FipsApi, FipsApiError, Flow, FlowClosed, Listener, default_socket_path
from .fipsdns import DEFAULT_DNS, resolve_npub
from . import discovery

HELLO_TIMEOUT = 30.0  # FIPS session setup plus a discovery retry ladder can take ~20 s
HELLO_RESEND = 3.0    # datagrams are unreliable and the far listener may not exist yet
MIN_HW_MTU = RNS.Reticulum.MTU


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_list(c, key) -> list[str]:
    value = c.get(key)
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        items = str(value).replace(",", " ").split()
    return [i.strip() for i in items if i.strip()]


def parse_peer(spec: str, default_port: int) -> tuple[str, int]:
    """``npub1...`` or ``npub1...:port``."""
    spec = spec.strip()
    if ":" in spec:
        npub, port = spec.rsplit(":", 1)
        return npub.strip(), int(port)
    return spec, default_port


class _PeerFlow:
    __slots__ = ("flow", "initiator", "created", "last_rx", "last_tx", "hello", "established")

    def __init__(self, flow: Flow, initiator: bool):
        now = time.time()
        self.flow = flow
        self.initiator = initiator
        self.created = now
        self.last_rx = now
        self.last_tx = now
        self.hello = None
        self.established = False

    @property
    def npub(self) -> str:
        return self.flow.peer


class FIPSInterface(Interface):
    DEFAULT_IFAC_SIZE = 16
    AUTOCONFIGURE_MTU = False
    FIXED_MTU = True
    BITRATE_GUESS = 10_000_000
    NOMINAL_MAX_PAYLOAD = 1170  # FIPS at its 1280-byte UDP default

    def __init__(self, owner, configuration):
        if os.name != "posix":
            raise OSError("FIPSInterface needs AF_UNIX with SCM_RIGHTS; not available on this platform")
        super().__init__()
        c = Interface.get_config_obj(configuration)
        self.owner = owner
        self.name = c["name"]

        self.fips_socket = c.get("fips_socket") or None
        port = c.get("listen_port", c.get("port", DEFAULT_PORT))
        self.listen_port = int(port)
        self.remote_port = int(c.get("remote_port", self.listen_port))
        self.peer_specs: dict[str, int] = {}
        for spec in _as_list(c, "peers"):
            npub, p = parse_peer(spec, self.remote_port)
            self.peer_specs[npub] = p
        self.allow = set(_as_list(c, "allow"))
        self.accept_unknown = _as_bool(c.get("accept_unknown"), False)
        self.ping_interval = float(c.get("ping_interval", 15))
        self.peer_timeout = float(c.get("peer_timeout", 60))
        self.reconnect_wait = float(c.get("reconnect_wait", 5))
        self.max_reconnect_wait = float(c.get("max_reconnect_wait", 120))
        self.mtu_override = int(c["mtu"]) if c.get("mtu") else None
        # Resolving <npub>.fips at the daemon's resolver primes its identity
        # cache, which FIPS builds before commit 55fb970 need before a native
        # flow to a multi-hop peer can complete discovery. "off" disables it.
        self.fips_dns = str(c.get("fips_dns", DEFAULT_DNS)).strip()
        self._primed: set[str] = set()
        # Discovery: announce this bridge over Reticulum and learn others.
        self.announce_enabled = _as_bool(c.get("announce"), True)
        self.announce_interval = float(c.get("announce_interval", 3600))
        self.auto_peer = _as_bool(c.get("auto_peer"), False)
        self.discovered: dict[str, dict] = {}   # npub -> {port, seen, via, tid}
        self.dynamic_peers: dict[str, int] = {}  # discovered npubs we dial (auto_peer)
        self._bridge_identity = None
        self._bridge_destination = None
        self._last_announce = 0.0
        self._announce_due = False
        self._announce_handler = None

        if self.listen_port < 1024 or self.listen_port > 65535:
            raise ValueError(f"listen_port {self.listen_port} is outside the application port tier 1024-65535")

        self.IN = True
        self.OUT = False
        self.online = True
        self.bitrate = self.BITRATE_GUESS
        self.HW_MTU = self.NOMINAL_MAX_PAYLOAD - 1 - self.DEFAULT_IFAC_SIZE
        self.spawned_interfaces: dict[str, FIPSPeerInterface] = {}
        self.peers: dict[str, _PeerFlow] = {}  # established, by npub; rnstatus shows len()
        self.nonce = os.urandom(8)
        self.tx_drops = 0

        self.lock = threading.RLock()
        self._api = FipsApi(self.fips_socket)
        self._listener: Listener | None = None
        self._flows: dict[int, _PeerFlow] = {}  # by fd, pending and established
        self._backoff: dict[str, tuple[float, float]] = {}  # npub -> (next_attempt, wait)
        self._dialing: set[str] = set()
        self._ops: collections.deque = collections.deque()
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._sel = selectors.DefaultSelector()
        self._sel.register(self._wake_r, selectors.EVENT_READ, "wake")
        self._stop = threading.Event()
        self._io_thread: threading.Thread | None = None
        self._sup_thread: threading.Thread | None = None
        self._api_warned = False

    # ---- Reticulum entry points -------------------------------------------------

    def _ifac_enabled(self) -> bool:
        return getattr(self, "ifac_identity", None) is not None

    def final_init(self):
        try:
            if self._ifac_enabled():
                self.HW_MTU = self.NOMINAL_MAX_PAYLOAD - 1 - self.ifac_size
            else:
                self.HW_MTU = self.NOMINAL_MAX_PAYLOAD - 1
            if self.mtu_override:
                self.HW_MTU = min(self.HW_MTU, self.mtu_override)
            self._io_thread = threading.Thread(target=self._io_loop, name=f"{self.name} io", daemon=True)
            self._sup_thread = threading.Thread(target=self._supervisor_loop, name=f"{self.name} sup", daemon=True)
            self._io_thread.start()
            self._sup_thread.start()
            RNS.log(f"{self} started, listening on FIPS port {self.listen_port}, {len(self.peer_specs)} configured peer(s)", RNS.LOG_VERBOSE)
        except Exception as e:
            RNS.log(f"{self} could not start: {e}", RNS.LOG_ERROR)
            self.online = False

    def process_outgoing(self, data):
        # The container carries no traffic; Transport broadcasts reach the children.
        pass

    def detach(self):
        self.detached = True
        self.online = False
        self._stop.set()
        self._wake()
        if self._io_thread is not None and self._io_thread is not threading.current_thread():
            self._io_thread.join(timeout=3)
        with self.lock:
            for pf in list(self._flows.values()):
                self._close_pf(pf)
            self._flows.clear()
            self.peers.clear()
            if self._listener is not None:
                self._listener.close()
                self._listener = None
            for child in list(self.spawned_interfaces.values()):
                child.flow = None
                child.online = False

    def teardown(self):
        self.detach()

    @property
    def clients(self):
        return len(self.spawned_interfaces)

    def __str__(self):
        return f"FIPSInterface[{self.name}]"

    # ---- stats aggregation (children report through parent_interface) -----------

    def received_announce(self, size=0, from_spawned=False):
        if from_spawned:
            self.ia_freq_deque.append(time.time())
            self.arxc += 1
            self.arxb += size

    def sent_announce(self, size=0, from_spawned=False):
        if from_spawned:
            self.oa_freq_deque.append(time.time())
            self.atxc += 1
            self.atxb += size

    def received_path_request(self, size=0, from_spawned=False):
        if from_spawned:
            self.ip_freq_deque.append(time.time())
            self.prxc += 1
            self.prxb += size

    def sent_path_request(self, size=0, from_spawned=False):
        if from_spawned:
            self.op_freq_deque.append(time.time())
            self.ptxc += 1
            self.ptxb += size

    # ---- helpers ----------------------------------------------------------------

    def _wake(self):
        try:
            self._wake_w.send(b"x")
        except OSError:
            pass

    def _post(self, *op):
        self._ops.append(op)
        self._wake()

    def _local_hw_mtu(self, flow: Flow) -> int:
        ifac = self.ifac_size if self._ifac_enabled() else 0
        hw = flow.max_payload - 1 - ifac
        if self.mtu_override:
            hw = min(hw, self.mtu_override)
        return hw

    def _allowed(self, npub: str) -> bool:
        if self.accept_unknown or npub in self.peer_specs or npub in self.allow:
            return True
        return self.auto_peer and npub in self.discovered

    def _dial_targets(self) -> dict[str, int]:
        targets = dict(self.dynamic_peers)
        targets.update(self.peer_specs)
        return targets

    @property
    def local_npub(self) -> str | None:
        listener = self._listener
        return listener.node if listener is not None else None

    def _hello_bytes(self, flow: Flow) -> bytes:
        tid = getattr(RNS.Transport, "identity", None)
        fields = {
            "impl": "rnsfips",
            "ver": __version__,
            "nonce": self.nonce,
            "hw_mtu": self._local_hw_mtu(flow),
            "tid": tid.hash if tid is not None else None,
        }
        return env.encode_hello(fields)

    def flow_failed(self, child: "FIPSPeerInterface", error: Exception):
        """Called from a child's send path on any thread."""
        self._post("lost", child.flow, f"send failed: {error}")

    # ---- supervisor thread: blocking setup calls --------------------------------

    def _supervisor_loop(self):
        while not self._stop.is_set():
            try:
                self._supervise_once()
            except Exception as e:
                RNS.log(f"{self} supervisor error: {e}", RNS.LOG_ERROR)
            self._stop.wait(1.0)

    def _supervise_once(self):
        now = time.time()
        if self._listener is None:
            try:
                listener = self._api.listen(self.listen_port)
                listener.sock.setblocking(False)
                self._post("listener", listener)
                self._api_warned = False
                RNS.log(f"{self} holding FIPS port {self.listen_port} on node {listener.node}", RNS.LOG_DEBUG)
            except FipsApiError as e:
                if not self._api_warned:
                    RNS.log(f"{self} cannot hold FIPS port {self.listen_port} via {self._api.socket_path}: {e.errno_name} {e.message}. Retrying.", RNS.LOG_WARNING)
                    self._api_warned = True
                return
        self._maybe_announce(now)
        with self.lock:
            targets = [
                (npub, port) for npub, port in self._dial_targets().items()
                if npub not in self.peers and npub not in self._dialing
                and not any(pf.npub == npub for pf in self._flows.values())
                and self._backoff.get(npub, (0, 0))[0] <= now
            ]
            for npub, _ in targets:
                self._dialing.add(npub)
        for npub, port in targets:
            self._prime_identity(npub)
            try:
                flow = self._api.connect(npub, port)
                flow.setblocking(False)
                self._post("flow", flow, True)
                RNS.log(f"{self} dialled {npub[:16]}… port {port}", RNS.LOG_DEBUG)
            except FipsApiError as e:
                self._schedule_retry(npub)
                RNS.log(f"{self} cannot open flow to {npub[:16]}…: {e.errno_name} {e.message}", RNS.LOG_DEBUG)
                with self.lock:
                    self._dialing.discard(npub)

    # ---- discovery over Reticulum announces -------------------------------------

    def _maybe_announce(self, now: float):
        if not self.announce_enabled or self.local_npub is None:
            return
        if not getattr(RNS.Transport, "ready", False):
            return
        if self._bridge_destination is None:
            try:
                path = os.path.join(RNS.Reticulum.identitypath, "rnsfips_bridge")
                identity = RNS.Identity.from_file(path) if os.path.isfile(path) else None
                if identity is None:
                    identity = RNS.Identity()
                    identity.to_file(path)
                self._bridge_identity = identity
                self._bridge_destination = RNS.Destination(
                    identity, RNS.Destination.IN, RNS.Destination.SINGLE, discovery.APP_NAME, discovery.ASPECT)
                self._bridge_destination.set_proof_strategy(RNS.Destination.PROVE_NONE)
                self._announce_handler = _BridgeAnnounceHandler(self)
                RNS.Transport.register_announce_handler(self._announce_handler)
                RNS.log(f"{self} bridge destination {RNS.prettyhexrep(self._bridge_destination.hash)}", RNS.LOG_VERBOSE)
            except Exception as e:
                RNS.log(f"{self} cannot set up discovery: {e}", RNS.LOG_ERROR)
                self.announce_enabled = False
                return
        due = (now - self._last_announce) > self.announce_interval
        if self._announce_due and (now - self._last_announce) > 30:
            due = True
        if not due:
            return
        tid = getattr(RNS.Transport, "identity", None)
        try:
            app_data = discovery.encode_app_data(self.local_npub, self.listen_port, tid.hash if tid else None, self.discovered)
        except ValueError as e:
            RNS.log(f"{self} cannot encode bridge announce ({e}); discovery disabled", RNS.LOG_ERROR)
            self.announce_enabled = False
            return
        self._bridge_destination.announce(app_data=app_data)
        self._last_announce = now
        self._announce_due = False

    def learn_bridge(self, npub: str, port: int, via: bytes | None, tid: bytes | None = None):
        """Record a bridge seen in an announce; dial it when auto_peer is on."""
        if npub == self.local_npub:
            return
        now = time.time()
        with self.lock:
            entry = self.discovered.get(npub) or {}
            entry.update({"port": port, "seen": now, "via": via.hex() if via else entry.get("via")})
            if tid:
                entry["tid"] = tid.hex()
            self.discovered[npub] = entry
            new = self.auto_peer and npub not in self.peer_specs and npub not in self.dynamic_peers
            if new:
                self.dynamic_peers[npub] = port
        if new:
            RNS.log(f"{self} discovered bridge {npub[:16]}… port {port}; dialling", RNS.LOG_VERBOSE)
            self._announce_due = True
        discovery.save_state(RNS.Reticulum.storagepath, self.discovered)

    def _prime_identity(self, npub: str):
        if self.fips_dns.lower() in ("off", "no", "false", "") or npub in self._primed:
            return
        addr = resolve_npub(npub, self.fips_dns, timeout=1.0)
        if addr:
            self._primed.add(npub)
            RNS.log(f"{self} primed FIPS identity for {npub[:16]}… ({addr})", RNS.LOG_DEBUG)
        else:
            RNS.log(f"{self} could not resolve {npub[:16]}….fips at {self.fips_dns}; multi-hop peers may need a newer FIPS", RNS.LOG_DEBUG)

    def _schedule_retry(self, npub: str):
        with self.lock:
            _, wait = self._backoff.get(npub, (0, 0))
            wait = min(max(self.reconnect_wait, wait * 2), self.max_reconnect_wait)
            self._backoff[npub] = (time.time() + wait, wait)

    # ---- I/O thread -------------------------------------------------------------

    def _io_loop(self):
        while not self._stop.is_set():
            try:
                events = self._sel.select(timeout=1.0)
            except OSError:
                if self._stop.is_set():
                    return
                time.sleep(0.1)
                continue
            for key, _ in events:
                if key.data == "wake":
                    self._drain_wake()
                elif key.data == "listener":
                    self._on_listener_readable()
                else:
                    self._on_flow_readable(key.fd)
            self._liveness()
        self._drain_wake()

    def _drain_wake(self):
        try:
            while self._wake_r.recv(4096):
                pass
        except (BlockingIOError, OSError):
            pass
        while self._ops:
            op = self._ops.popleft()
            try:
                self._handle_op(op)
            except Exception as e:
                RNS.log(f"{self} error handling {op[0]}: {e}", RNS.LOG_ERROR)

    def _handle_op(self, op):
        kind = op[0]
        if kind == "listener":
            listener = op[1]
            if self._stop.is_set():
                listener.close()
                return
            with self.lock:
                if self._listener is not None:
                    self._listener.close()
                self._listener = listener
            self._sel.register(listener.sock, selectors.EVENT_READ, "listener")
        elif kind == "flow":
            flow, initiator = op[1], op[2]
            with self.lock:
                self._dialing.discard(flow.peer)
            if self._stop.is_set():
                flow.close()
                return
            self._add_flow(flow, initiator)
        elif kind == "lost":
            flow, reason = op[1], op[2]
            if flow is None:
                return
            for pf in list(self._flows.values()):
                if pf.flow is flow:
                    self._lose(pf, reason)
                    break

    def _add_flow(self, flow: Flow, initiator: bool):
        pf = _PeerFlow(flow, initiator)
        with self.lock:
            self._flows[flow.fileno()] = pf
        self._sel.register(flow.sock, selectors.EVENT_READ, "flow")
        try:
            flow.send(self._hello_bytes(flow))
            pf.last_tx = time.time()
        except OSError as e:
            self._lose(pf, f"could not send HELLO: {e}")

    def _on_listener_readable(self):
        listener = self._listener
        if listener is None:
            return
        try:
            flow = listener.accept()
        except BlockingIOError:
            return
        except (FlowClosed, OSError) as e:
            RNS.log(f"{self} FIPS listener lost ({e}); will re-listen", RNS.LOG_WARNING)
            self._sel_unregister(listener.sock)
            listener.close()
            with self.lock:
                if self._listener is listener:
                    self._listener = None
            return
        except FipsApiError as e:
            RNS.log(f"{self} bad arrival on listener: {e}", RNS.LOG_DEBUG)
            return
        if not self._allowed(flow.peer):
            RNS.log(f"{self} refusing flow from unlisted peer {flow.peer[:16]}…", RNS.LOG_DEBUG)
            flow.close()
            return
        flow.setblocking(False)
        self._add_flow(flow, initiator=False)

    def _on_flow_readable(self, fd: int):
        pf = self._flows.get(fd)
        if pf is None:
            return
        try:
            datagram = pf.flow.recv()
        except BlockingIOError:
            return
        except FlowClosed:
            self._lose(pf, "closed by daemon")
            return
        except OSError as e:
            self._lose(pf, f"recv failed: {e}")
            return
        pf.last_rx = time.time()
        try:
            kind, payload = env.split(datagram)
        except env.EnvelopeError:
            return
        if kind == env.RNS_FRAME:
            if pf.established:
                child = self.spawned_interfaces.get(pf.npub)
                if child is not None and child.flow is pf.flow:
                    child.process_incoming(payload)
        elif kind == env.PING:
            self._send_raw(pf, env.pong())
        elif kind == env.PONG:
            pass
        elif kind == env.HELLO:
            self._on_hello(pf, payload)
        else:
            child = self.spawned_interfaces.get(pf.npub) if pf.established else None
            (child or self).protocol_violation(f"unknown envelope type 0x{kind:02x}")

    def _send_raw(self, pf: _PeerFlow, datagram: bytes):
        try:
            pf.flow.send(datagram)
            pf.last_tx = time.time()
        except BlockingIOError:
            pass
        except OSError as e:
            self._lose(pf, f"send failed: {e}")

    def _on_hello(self, pf: _PeerFlow, payload: bytes):
        try:
            hello = env.decode_hello(payload)
        except env.EnvelopeError as e:
            RNS.log(f"{self} rejecting flow from {pf.npub[:16]}…: {e}", RNS.LOG_DEBUG)
            self._lose(pf, "bad HELLO")
            return
        if pf.established:
            child = self.spawned_interfaces.get(pf.npub)
            if child is not None and hello.get("nonce") != pf.hello.get("nonce"):
                RNS.log(f"{child} peer restarted, re-synthesizing tunnel", RNS.LOG_DEBUG)
                pf.hello = hello
                RNS.Transport.synthesize_tunnel(child)
            return
        pf.hello = hello
        npub = pf.npub
        remote_hw = int(hello.get("hw_mtu") or 0)
        hw_mtu = min(self._local_hw_mtu(pf.flow), remote_hw) if remote_hw else self._local_hw_mtu(pf.flow)
        if hw_mtu < MIN_HW_MTU:
            RNS.log(f"{self} flow to {npub[:16]}… offers MTU {hw_mtu} < {MIN_HW_MTU}; refusing", RNS.LOG_WARNING)
            self._lose(pf, "MTU too small")
            return

        with self.lock:
            existing = self.peers.get(npub)
            if existing is not None and existing is not pf:
                same_instance = existing.hello is not None and existing.hello.get("nonce") == hello.get("nonce")
                if not same_instance:
                    # A different nonce means the peer's rnsd restarted: its old flow
                    # is dead on their side. The new flow wins, whoever dialled it.
                    RNS.log(f"{self} peer {npub[:16]}… restarted; replacing its old flow", RNS.LOG_DEBUG)
                    self._close_pf(existing, forget=True)
                else:
                    local = pf.flow.node
                    keep_initiator = local < npub  # simultaneous dials: the smaller npub's dial wins
                    if pf.initiator == keep_initiator:
                        RNS.log(f"{self} replacing duplicate flow to {npub[:16]}…", RNS.LOG_DEBUG)
                        self._close_pf(existing, forget=True)
                    else:
                        RNS.log(f"{self} dropping duplicate flow to {npub[:16]}…", RNS.LOG_DEBUG)
                        self._close_pf(pf, forget=True)
                        return
            child = self.spawned_interfaces.get(npub)
            if child is None:
                child = self._spawn_child(npub)
            pf.established = True
            self.peers[npub] = pf
            self._backoff.pop(npub, None)
        child.attach_flow(pf.flow, hw_mtu)
        self._announce_due = True
        RNS.log(f"{child} up (MTU {hw_mtu}, {'dialled' if pf.initiator else 'accepted'}, impl {hello.get('impl')} {hello.get('ver')})", RNS.LOG_VERBOSE)

    def _spawn_child(self, npub: str) -> "FIPSPeerInterface":
        child = FIPSPeerInterface(self, npub, configured=npub in self.peer_specs or npub in self.dynamic_peers)
        child.OUT = self.OUT
        child.IN = self.IN
        child.ingress_control = self.ingress_control
        child.ic_max_held_announces = self.ic_max_held_announces
        child.ic_burst_hold = self.ic_burst_hold
        child.ic_burst_freq = self.ic_burst_freq
        child.ic_burst_freq_new = self.ic_burst_freq_new
        child.ic_new_time = self.ic_new_time
        child.ic_burst_penalty = self.ic_burst_penalty
        child.ic_held_release_interval = self.ic_held_release_interval
        child.egress_control = self.egress_control
        child.ec_pr_freq = self.ec_pr_freq
        child.ic_pr_burst_freq_new = self.ic_pr_burst_freq_new
        child.ic_pr_burst_freq = self.ic_pr_burst_freq
        child.parent_interface = self
        child.bitrate = self.bitrate
        child.ifac_size = self.ifac_size
        child.ifac_netname = self.ifac_netname
        child.ifac_netkey = self.ifac_netkey
        if self._ifac_enabled():
            child.ifac_key = self.ifac_key
            child.ifac_identity = self.ifac_identity
            child.ifac_signature = self.ifac_signature
        child.announce_rate_target = self.announce_rate_target
        child.announce_rate_grace = self.announce_rate_grace
        child.announce_rate_penalty = self.announce_rate_penalty
        child.mode = self.mode
        child.gravity = self.gravity
        child.recursive_prs = self.recursive_prs
        child.announces_from_internal = self.announces_from_internal
        child.announces_to_internal = self.announces_to_internal
        child.announce_cap = self.announce_cap
        child.HW_MTU = self.HW_MTU
        RNS.Transport.add_interface(child)
        self.spawned_interfaces[npub] = child
        child.spawned_at = time.time()
        return child

    def _sel_unregister(self, sock):
        try:
            self._sel.unregister(sock)
        except (KeyError, ValueError, OSError):
            pass

    def _close_pf(self, pf: _PeerFlow, forget: bool = True):
        self._sel_unregister(pf.flow.sock)
        for fd, other in list(self._flows.items()):
            if other is pf:
                del self._flows[fd]
        pf.flow.close()

    def _lose(self, pf: _PeerFlow, reason: str):
        npub = pf.npub
        with self.lock:
            self._close_pf(pf)
            was_established = pf.established and self.peers.get(npub) is pf
            if was_established:
                del self.peers[npub]
            child = self.spawned_interfaces.get(npub) if was_established else None
        if child is not None and child.flow is pf.flow:
            RNS.log(f"{child} down: {reason}", RNS.LOG_VERBOSE)
            child.detach_flow()
            if not child.configured:
                child.detach()
                child.teardown()
        elif not pf.established:
            RNS.log(f"{self} flow to {npub[:16]}… dropped before HELLO: {reason}", RNS.LOG_DEBUG)
        if npub in self.peer_specs or npub in self.dynamic_peers:
            self._schedule_retry(npub)

    def _liveness(self):
        now = time.time()
        for pf in list(self._flows.values()):
            if not pf.established:
                if now - pf.created > HELLO_TIMEOUT:
                    self._lose(pf, "no HELLO")
                elif now - pf.last_tx > HELLO_RESEND:
                    self._send_raw(pf, self._hello_bytes(pf.flow))
                continue
            if now - pf.last_rx > self.peer_timeout:
                self._lose(pf, f"silent for {self.peer_timeout:.0f}s")
            elif now - max(pf.last_rx, pf.last_tx) > self.ping_interval:
                self._send_raw(pf, env.ping())


class FIPSPeerInterface(Interface):
    """One FIPS peer. Spawned by :class:`FIPSInterface`, registered with Transport."""

    AUTOCONFIGURE_MTU = False
    FIXED_MTU = True

    def __init__(self, parent: FIPSInterface, npub: str, configured: bool):
        super().__init__()
        self.parent_interface = parent
        self.owner = parent.owner
        self.npub = npub
        self.configured = configured
        self.initiator = configured
        self.name = f"{parent.name} peer {npub[5:17]}"
        self.flow: Flow | None = None
        self.online = False
        self.tx_drops = 0
        self.wants_tunnel = False
        self.had_flow = False
        self.spawned_at = 0

    def attach_flow(self, flow: Flow, hw_mtu: int):
        reconnect = self.had_flow
        self.had_flow = True
        self.flow = flow
        self.HW_MTU = hw_mtu
        self.online = True
        if reconnect:
            # Transport only honours wants_tunnel at startup; a reconnect must
            # synthesize the tunnel itself, as BackboneClientInterface does.
            RNS.Transport.synthesize_tunnel(self)

    def detach_flow(self):
        self.online = False
        self.flow = None

    def process_outgoing(self, data):
        flow = self.flow
        if flow is None or not self.online or self.detached:
            return
        datagram = env.rns_frame(data)
        if len(datagram) > flow.max_payload:
            self.tx_drops += 1
            RNS.log(f"{self} dropping {len(data)}-byte frame above flow payload limit {flow.max_payload}", RNS.LOG_DEBUG)
            return
        try:
            flow.send(datagram)
        except BlockingIOError:
            self.tx_drops += 1
            return
        except OSError as e:
            if e.errno in (errno.EMSGSIZE,):
                self.tx_drops += 1
                return
            self.parent_interface.flow_failed(self, e)
            return
        self.txb += len(data)
        self.parent_interface.txb += len(data)

    def process_incoming(self, data):
        if self.detached or not self.online:
            return
        self.rxb += len(data)
        self.parent_interface.rxb += len(data)
        self.owner.inbound(data, self)

    def detach(self):
        self.detached = True
        self.online = False
        flow = self.flow
        self.flow = None
        if flow is not None:
            parent = self.parent_interface
            with parent.lock:
                pf = parent.peers.get(self.npub)
                if pf is not None and pf.flow is flow:
                    del parent.peers[self.npub]
                parent._close_pf(pf) if pf is not None else flow.close()

    def teardown(self):
        self.online = False
        self.OUT = False
        self.IN = False
        parent = self.parent_interface
        with parent.lock:
            if parent.spawned_interfaces.get(self.npub) is self:
                del parent.spawned_interfaces[self.npub]
        if not self.configured:
            RNS.Transport.remove_interface(self)

    def __str__(self):
        return f"FIPSPeerInterface[{self.parent_interface.name}/{self.npub}]"


class _BridgeAnnounceHandler:
    """Learns other bridges from their ``rnsfips.bridge`` announces."""

    def __init__(self, interface: FIPSInterface):
        self.interface = interface
        self.aspect_filter = f"{discovery.APP_NAME}.{discovery.ASPECT}"
        self.receive_path_responses = True

    def received_announce(self, destination_hash, announced_identity, app_data):
        info = discovery.decode_app_data(app_data)
        if info is None:
            return
        iface = self.interface
        iface.learn_bridge(info["npub"], info["port"], destination_hash, info["tid"])
        for npub, port in info["bridges"]:
            iface.learn_bridge(npub, port, None)
