"""Client for the FIPS control socket (the one ``fipsctl`` talks to).

One request per connection, one JSON object per line each way, per
FIPS ``docs/reference/control-socket.md``.
"""

from __future__ import annotations

import json
import os
import socket

_MAX_REQUEST = 4096


def default_control_path() -> str:
    candidates = ["/run/fips/control.sock", "/var/run/fips/control.sock"]
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        candidates.append(os.path.join(xdg, "fips", "control.sock"))
    candidates.append("/tmp/fips-control.sock")
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]


class FipsControlError(RuntimeError):
    pass


class FipsControl:
    def __init__(self, socket_path: str | None = None, timeout: float = 5.0):
        self.socket_path = socket_path or default_control_path()
        self.timeout = timeout

    def query(self, command: str, params: dict | None = None) -> dict:
        req = {"command": command}
        if params:
            req["params"] = params
        line = json.dumps(req).encode("utf-8") + b"\n"
        if len(line) > _MAX_REQUEST:
            raise FipsControlError("request too large")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(self.timeout)
            s.connect(self.socket_path)
            s.sendall(line)
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        except OSError as e:
            raise FipsControlError(f"{self.socket_path}: {e}") from e
        finally:
            s.close()
        if not buf:
            raise FipsControlError("empty reply")
        try:
            reply = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        except Exception as e:
            raise FipsControlError(f"malformed reply: {e}") from e
        if reply.get("status") != "ok":
            raise FipsControlError(reply.get("message", "unknown error"))
        return reply.get("data") or {}

    # Convenience wrappers for the commands the bridge uses.
    def show_status(self) -> dict:
        return self.query("show_status")

    def show_peers(self) -> list:
        return self.query("show_peers").get("peers", [])

    def show_transports(self) -> list:
        return self.query("show_transports").get("transports", [])

    def show_native_flows(self) -> dict:
        return self.query("show_native_flows")

    def connect(self, npub: str, address: str, transport: str = "udp") -> dict:
        return self.query("connect", {"npub": npub, "address": address, "transport": transport})

    def disconnect(self, npub: str) -> dict:
        return self.query("disconnect", {"npub": npub})
