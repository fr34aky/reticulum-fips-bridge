"""A fake FIPS daemon exposing the native datagram API, for tests.

One :class:`FakeFips` per simulated node. Nodes sharing a :class:`Mesh`
can reach each other by ``(npub, port)``. The line protocol, descriptor
passing and arrival message follow FIPS ``docs/reference/native-api.md``
closely enough that the real client code is exercised unchanged.

Fault injection: ``max_payload``, ``drop_every_n``, ``restart()``.
"""

from __future__ import annotations

import array
import json
import os
import selectors
import socket
import threading
import time
from dataclasses import dataclass, field


def fake_npub(seed: str) -> str:
    """Deterministic, valid npub (bech32 of a 32-byte key derived from the seed)."""
    import hashlib

    from rnsfips.bech32 import bytes_to_npub

    return bytes_to_npub(hashlib.sha256(seed.encode()).digest())


class Mesh:
    def __init__(self):
        self.nodes: dict[str, "FakeFips"] = {}
        self.lock = threading.Lock()

    def add(self, node: "FakeFips"):
        with self.lock:
            self.nodes[node.npub] = node

    def get(self, npub: str):
        with self.lock:
            return self.nodes.get(npub)


@dataclass
class _FlowRec:
    daemon_sock: socket.socket
    local_port: int
    peer: str
    remote_port: int
    flow_id: int
    pending: list = field(default_factory=list)


class FakeFips:
    def __init__(self, socket_path: str, npub: str, mesh: Mesh, max_payload: int = 1170,
                 drop_every_n: int = 0, backlog: int = 16):
        self.socket_path = socket_path
        self.npub = npub
        self.mesh = mesh
        self.max_payload = max_payload
        self.drop_every_n = drop_every_n
        self.backlog = backlog
        self.lock = threading.RLock()
        self.listeners: dict[int, socket.socket] = {}
        self.flows: dict[int, _FlowRec] = {}  # keyed by daemon_sock fileno
        self.stats = {"sent": 0, "received": 0, "dropped_oversize": 0, "dropped_fault": 0, "dropped_noroute": 0}
        self._next_flow = 1
        self._next_port = 49152
        self._sel = selectors.DefaultSelector()
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._sel.register(self._wake_r, selectors.EVENT_READ, ("wake", None))
        self._stop = threading.Event()
        self._srv: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        mesh.add(self)

    # ---- lifecycle --------------------------------------------------------------

    def start(self):
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.socket_path)
        self._srv.listen(16)
        self._srv.settimeout(0.2)
        t1 = threading.Thread(target=self._accept_loop, daemon=True, name=f"fakefips-accept-{self.npub[5:11]}")
        t2 = threading.Thread(target=self._io_loop, daemon=True, name=f"fakefips-io-{self.npub[5:11]}")
        self._threads = [t1, t2]
        t1.start()
        t2.start()
        return self

    def stop(self):
        self._stop.set()
        self._wake()
        for t in self._threads:
            t.join(timeout=2)
        self.restart(close_only=True)
        if self._srv:
            self._srv.close()
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass

    def restart(self, close_only: bool = False):
        """Drop every flow and listener, as a daemon restart would."""
        with self.lock:
            for rec in list(self.flows.values()):
                self._unregister(rec.daemon_sock)
                rec.daemon_sock.close()
            self.flows.clear()
            for s in list(self.listeners.values()):
                self._unregister(s)
                s.close()
            self.listeners.clear()

    def _wake(self):
        try:
            self._wake_w.send(b"x")
        except OSError:
            pass

    def _unregister(self, sock):
        try:
            self._sel.unregister(sock)
        except (KeyError, ValueError, OSError):
            pass

    # ---- setup connections ---------------------------------------------------------

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()

    def _serve_conn(self, conn: socket.socket):
        conn.settimeout(5)
        buf = b""
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                buf += chunk
                if len(buf) > 8192:
                    return  # too large: drop connection, as the daemon does
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    reply, fd = self._handle(line)
                    anc = []
                    if fd is not None:
                        anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]).tobytes())]
                    conn.sendmsg([json.dumps(reply).encode() + b"\n"], anc)
                    if fd is not None:
                        os.close(fd)
        except OSError:
            pass
        finally:
            conn.close()

    def _error(self, errno_name: str, message: str):
        return {"status": "error", "data": {"errno": errno_name}, "message": message}, None

    def _handle(self, line: bytes):
        try:
            req = json.loads(line.decode())
        except Exception:
            return self._error("EINVAL", "malformed command")
        command = req.get("command")
        params = req.get("params")
        if params is None:
            return self._error("EINVAL", f"command '{command}' requires params")
        if command == "listen":
            return self._listen(params)
        if command == "connect":
            return self._connect(params)
        return self._error("ENOSYS", f"unknown command '{command}'")

    def _listen(self, params):
        port = int(params.get("local_port", 0))
        if port < 1024 or port > 65535:
            return self._error("EADDRNOTAVAIL", f"port {port} is reserved")
        with self.lock:
            if port in self.listeners:
                return self._error("EADDRINUSE", f"port {port} already held")
            d, c = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            d.setblocking(False)
            self.listeners[port] = d
            self._sel.register(d, selectors.EVENT_READ, ("listener", port))
        self._wake()
        fd = os.dup(c.fileno())
        c.close()
        return {"status": "ok", "data": {"local_port": port, "node": self.npub, "backlog": self.backlog}}, fd

    def _connect(self, params):
        peer = params.get("peer")
        remote_port = int(params.get("remote_port", 0))
        local_port = params.get("local_port")
        if not isinstance(peer, str) or not peer.startswith("npub1"):
            return self._error("EINVAL", "peer is not an npub")
        if remote_port < 1024:
            return self._error("EADDRNOTAVAIL", f"port {remote_port} is reserved")
        with self.lock:
            if local_port is None:
                local_port = self._next_port
                self._next_port += 1
            local_port = int(local_port)
            rec = self._new_flow(local_port, peer, remote_port)
        fd = os.dup(rec.client_fd)
        os.close(rec.client_fd)
        rec.client_fd = None
        return {"status": "ok", "data": {
            "flow_id": rec.flow_id, "peer": peer, "node": self.npub,
            "local_port": local_port, "remote_port": remote_port, "max_payload": self.max_payload}}, fd

    def _new_flow(self, local_port, peer, remote_port):
        d, c = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        d.setblocking(False)
        rec = _FlowRec(d, local_port, peer, remote_port, self._next_flow)
        rec.client_fd = os.dup(c.fileno())
        c.close()
        self._next_flow += 1
        self.flows[d.fileno()] = rec
        self._sel.register(d, selectors.EVENT_READ, ("flow", d.fileno()))
        self._wake()
        return rec

    # ---- data plane ----------------------------------------------------------------

    def _io_loop(self):
        while not self._stop.is_set():
            try:
                events = self._sel.select(timeout=0.5)
            except OSError:
                continue
            for key, _ in events:
                kind, ident = key.data
                if kind == "wake":
                    try:
                        self._wake_r.recv(4096)
                    except OSError:
                        pass
                elif kind == "flow":
                    self._on_flow(ident)
                elif kind == "listener":
                    # A client never writes on a listener; a readable listener means
                    # the client closed it.
                    with self.lock:
                        s = self.listeners.pop(ident, None)
                        if s is not None:
                            self._unregister(s)
                            s.close()

    def _on_flow(self, fd):
        with self.lock:
            rec = self.flows.get(fd)
        if rec is None:
            return
        try:
            data = rec.daemon_sock.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            with self.lock:
                self.flows.pop(fd, None)
                self._unregister(rec.daemon_sock)
                rec.daemon_sock.close()
            return
        self.stats["received"] += 1
        if len(data) > self.max_payload:
            self.stats["dropped_oversize"] += 1
            return
        if self.drop_every_n and self.stats["received"] % self.drop_every_n == 0:
            self.stats["dropped_fault"] += 1
            return
        self._route(rec, data)

    def _route(self, rec: _FlowRec, data: bytes):
        target = self.mesh.get(rec.peer)
        if target is None:
            self.stats["dropped_noroute"] += 1
            return
        target.deliver(self.npub, rec.local_port, rec.remote_port, data)
        self.stats["sent"] += 1

    def deliver(self, src_npub: str, src_port: int, dst_port: int, data: bytes):
        with self.lock:
            for rec in self.flows.values():
                if rec.local_port == dst_port and rec.peer == src_npub and rec.remote_port == src_port:
                    try:
                        rec.daemon_sock.send(data)
                    except OSError:
                        pass
                    return
            listener = self.listeners.get(dst_port)
            if listener is None:
                self.stats["dropped_noroute"] += 1
                return
            rec = self._new_flow(dst_port, src_npub, src_port)
            try:
                rec.daemon_sock.send(data)  # held datagram, written before the arrival
            except OSError:
                pass
            arrival = {
                "flow_id": rec.flow_id, "peer": src_npub, "node": self.npub,
                "local_port": dst_port, "remote_port": src_port,
                "max_payload": self.max_payload, "held": 1,
            }
            anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [rec.client_fd]).tobytes())]
            try:
                listener.sendmsg([json.dumps(arrival).encode()], anc)
            except OSError:
                pass
            os.close(rec.client_fd)
            rec.client_fd = None


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class FakeControl:
    """Fake FIPS control socket: answers show_status, records connect/disconnect."""

    def __init__(self, socket_path: str, npub: str):
        self.socket_path = socket_path
        self.npub = npub
        self.calls: list[tuple[str, dict]] = []
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._srv = None
        self._thread = None

    def start(self):
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.socket_path)
        self._srv.listen(8)
        self._srv.settimeout(0.2)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._srv:
            self._srv.close()
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                conn.settimeout(2)
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf.split(b"\n", 1)[0].decode() or "{}")
                cmd, params = req.get("command"), req.get("params") or {}
                with self.lock:
                    self.calls.append((cmd, params))
                if cmd == "show_status":
                    reply = {"status": "ok", "data": {"npub": self.npub, "state": "running", "peer_count": 0}}
                elif cmd in ("connect", "disconnect"):
                    reply = {"status": "ok", "data": {"refreshed": False}}
                else:
                    reply = {"status": "error", "message": f"unknown command {cmd}"}
                conn.sendall(json.dumps(reply).encode() + b"\n")
            except Exception:
                pass
            finally:
                conn.close()

    def connects(self) -> list[dict]:
        with self.lock:
            return [p for c, p in self.calls if c == "connect"]
