"""Client for the FIPS native datagram API.

This speaks the line protocol documented in FIPS ``docs/reference/native-api.md``
and depends on nothing from the FIPS tree. Summary of that protocol:

* Setup: one ``AF_UNIX SOCK_STREAM`` connection to ``api.sock``, one JSON
  object per line in, one JSON object per line back. A reply that carries a
  descriptor passes it as ``SCM_RIGHTS`` ancillary data on the same message.
* A flow or a listener is an ``AF_UNIX SOCK_SEQPACKET`` descriptor. One
  ``recv`` is one datagram. There is no framing and no far-end close.
* An arriving flow is one message on the listener descriptor: a JSON object
  (no newline) plus the new flow's descriptor.

Linux and FreeBSD only, like the daemon side.
"""

from __future__ import annotations

import array
import errno
import json
import os
import socket
from dataclasses import dataclass, field

DEFAULT_PORT = 21075  # 'RS'
_MAX_LINE = 8192
_CMSG_SPACE = socket.CMSG_SPACE(16 * array.array("i").itemsize) if hasattr(socket, "CMSG_SPACE") else 0
_CLOEXEC = getattr(socket, "MSG_CMSG_CLOEXEC", 0)


def default_socket_path() -> str:
    """Mirror the daemon's own resolution order for ``api.sock``."""
    candidates = ["/run/fips/api.sock", "/var/run/fips/api.sock"]
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        candidates.append(os.path.join(xdg, "fips", "api.sock"))
    candidates.append("/tmp/fips-api.sock")
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]


class FipsApiError(OSError):
    """A refusal from the daemon, carrying the errno name it chose."""

    def __init__(self, errno_name: str, message: str = ""):
        code = getattr(errno, errno_name, errno.ECONNREFUSED) if errno_name else errno.ECONNREFUSED
        super().__init__(code, message or errno_name)
        self.errno_name = errno_name or "ECONNREFUSED"
        self.message = message


class FlowClosed(ConnectionError):
    """The daemon closed its half of a flow or listener."""


def _recv_fds(sock: socket.socket, bufsize: int) -> tuple[bytes, list[int], int]:
    data, ancdata, flags, _ = sock.recvmsg(bufsize, _CMSG_SPACE, _CLOEXEC)
    fds: list[int] = []
    for level, ctype, cdata in ancdata:
        if level == socket.SOL_SOCKET and ctype == socket.SCM_RIGHTS:
            arr = array.array("i")
            arr.frombytes(cdata[: len(cdata) - (len(cdata) % arr.itemsize)])
            fds.extend(arr)
    return data, fds, flags


def _close_fds(fds):
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


@dataclass
class Flow:
    """One flow to one peer. Owns a ``SOCK_SEQPACKET`` descriptor."""

    sock: socket.socket
    peer: str
    node: str
    local_port: int
    remote_port: int
    max_payload: int
    flow_id: int | None = None
    held: int = 0
    _closed: bool = field(default=False, init=False, repr=False)

    def fileno(self) -> int:
        return self.sock.fileno()

    def setblocking(self, flag: bool) -> None:
        self.sock.setblocking(flag)

    def send(self, data: bytes) -> None:
        if len(data) > self.max_payload:
            raise OSError(errno.EMSGSIZE, f"datagram of {len(data)} bytes exceeds max_payload {self.max_payload}")
        if not data:
            raise ValueError("empty datagrams are not sent: a zero-length read means the flow is closed")
        self.sock.send(data, getattr(socket, "MSG_NOSIGNAL", 0))

    def recv(self, bufsize: int | None = None) -> bytes:
        """Receive one datagram. Raises :class:`FlowClosed` on a zero-length read."""
        bufsize = bufsize or (self.max_payload + 1)
        data, _, flags, _ = self.sock.recvmsg(bufsize)
        if not data:
            raise FlowClosed(f"flow to {self.peer}:{self.remote_port} closed by daemon")
        if flags & getattr(socket, "MSG_TRUNC", 0):
            raise OSError(errno.EMSGSIZE, "truncated datagram")
        return data

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self.sock.close()
            except OSError:
                pass

    @property
    def closed(self) -> bool:
        return self._closed


@dataclass
class Listener:
    """A held port. Arrivals are delivered as messages on its descriptor."""

    sock: socket.socket
    node: str
    local_port: int
    _closed: bool = field(default=False, init=False, repr=False)

    def fileno(self) -> int:
        return self.sock.fileno()

    def accept(self) -> Flow:
        """Take one arriving flow. Blocks unless the socket is non-blocking."""
        data, fds, _ = _recv_fds(self.sock, _MAX_LINE)
        if not data and not fds:
            raise FlowClosed(f"listener on port {self.local_port} closed by daemon")
        if not fds:
            raise FipsApiError("EPROTO", "arrival carried no descriptor")
        if len(fds) > 1:
            _close_fds(fds[1:])
        try:
            arrival = json.loads(data.decode("utf-8"))
        except Exception as e:
            _close_fds(fds[:1])
            raise FipsApiError("EPROTO", f"malformed arrival: {e}") from e
        sock = socket.socket(fileno=fds[0])
        return Flow(
            sock=sock,
            peer=arrival["peer"],
            node=arrival.get("node", self.node),
            local_port=int(arrival.get("local_port", self.local_port)),
            remote_port=int(arrival["remote_port"]),
            max_payload=int(arrival["max_payload"]),
            flow_id=arrival.get("flow_id"),
            held=int(arrival.get("held", 0)),
        )

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self.sock.close()
            except OSError:
                pass


class FipsApi:
    """Setup-call client. One connection per command, closed after the reply."""

    def __init__(self, socket_path: str | None = None, timeout: float = 5.0):
        self.socket_path = socket_path or default_socket_path()
        self.timeout = timeout

    def _command(self, command: str, params: dict) -> tuple[dict, list[int]]:
        line = json.dumps({"command": command, "params": params}).encode("utf-8") + b"\n"
        if len(line) > _MAX_LINE:
            raise FipsApiError("EMSGSIZE", "command line too long")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        fds: list[int] = []
        try:
            s.settimeout(self.timeout)
            s.connect(self.socket_path)
            s.sendall(line)
            buf = b""
            while b"\n" not in buf:
                data, got, _ = _recv_fds(s, _MAX_LINE)
                fds.extend(got)
                if not data:
                    _close_fds(fds)
                    raise FipsApiError("ECONNRESET", "daemon closed the connection without a reply")
                buf += data
                if len(buf) > _MAX_LINE:
                    _close_fds(fds)
                    raise FipsApiError("EPROTO", "reply line too long")
            reply_line = buf.split(b"\n", 1)[0]
        except socket.timeout as e:
            _close_fds(fds)
            raise FipsApiError("ETIMEDOUT", str(e)) from e
        except (ConnectionRefusedError, FileNotFoundError) as e:
            _close_fds(fds)
            raise FipsApiError("ECONNREFUSED", f"{self.socket_path}: {e}") from e
        finally:
            # The daemon keeps its copy of the descriptor until this connection
            # closes, so the fd we were handed is safe once we have read it.
            s.close()
        try:
            reply = json.loads(reply_line.decode("utf-8"))
        except Exception as e:
            _close_fds(fds)
            raise FipsApiError("EPROTO", f"malformed reply: {e}") from e
        if reply.get("status") != "ok":
            _close_fds(fds)
            data = reply.get("data") or {}
            raise FipsApiError(data.get("errno") or "ECONNREFUSED", reply.get("message", ""))
        return reply.get("data") or {}, fds

    def connect(self, peer: str, remote_port: int, local_port: int | None = None) -> Flow:
        params = {"peer": peer, "remote_port": int(remote_port)}
        if local_port is not None:
            params["local_port"] = int(local_port)
        data, fds = self._command("connect", params)
        if not fds:
            raise FipsApiError("EPROTO", "connect reply carried no descriptor")
        if len(fds) > 1:
            _close_fds(fds[1:])
        return Flow(
            sock=socket.socket(fileno=fds[0]),
            peer=data.get("peer", peer),
            node=data.get("node", ""),
            local_port=int(data.get("local_port", local_port or 0)),
            remote_port=int(data.get("remote_port", remote_port)),
            max_payload=int(data["max_payload"]),
            flow_id=data.get("flow_id"),
        )

    def listen(self, local_port: int) -> Listener:
        data, fds = self._command("listen", {"local_port": int(local_port)})
        if not fds:
            raise FipsApiError("EPROTO", "listen reply carried no descriptor")
        if len(fds) > 1:
            _close_fds(fds[1:])
        return Listener(
            sock=socket.socket(fileno=fds[0]),
            node=data.get("node", ""),
            local_port=int(data.get("local_port", local_port)),
        )
