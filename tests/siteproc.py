"""Spawn and drive ``tests/site.py`` subprocesses."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SHIM = os.path.join(ROOT, "src", "rnsfips", "data", "FIPSInterface.py")


def write_config(configdir: str, fips_socket: str | None, listen_port: int, peers: list[str],
                 mode: str = "full", extra: str = "", transport: bool = True, allow: list[str] | None = None,
                 accept_unknown: bool = False, name: str = "FIPS", ifac: tuple[str, str] | None = None,
                 extra_interfaces: str = ""):
    os.makedirs(os.path.join(configdir, "interfaces"), exist_ok=True)
    shutil.copyfile(SHIM, os.path.join(configdir, "interfaces", "FIPSInterface.py"))
    lines = [
        "[reticulum]",
        f"  enable_transport = {'Yes' if transport else 'No'}",
        "  share_instance = No",
        "  panic_on_interface_error = No",
        "[logging]",
        "  loglevel = 6",
        "[interfaces]",
    ]
    if extra_interfaces:
        lines.extend("  " + l for l in textwrap.dedent(extra_interfaces).strip().splitlines())
    if fips_socket is None:
        with open(os.path.join(configdir, "config"), "w") as f:
            f.write("\n".join(lines) + "\n")
        return
    lines += [
        f"  [[{name}]]",
        "    type = FIPSInterface",
        "    enabled = yes",
        f"    mode = {mode}",
        f"    fips_socket = {fips_socket}",
        f"    listen_port = {listen_port}",
        "    ping_interval = 1",
        "    peer_timeout = 4",
        "    reconnect_wait = 0.5",
        "    fips_dns = off",
    ]
    if peers:
        lines.append("    peers = " + ", ".join(peers))
    if allow:
        lines.append("    allow = " + ", ".join(allow))
    if accept_unknown:
        lines.append("    accept_unknown = yes")
    if ifac:
        lines.append(f"    network_name = {ifac[0]}")
        lines.append(f"    passphrase = {ifac[1]}")
    if extra:
        lines.extend("    " + l for l in textwrap.dedent(extra).strip().splitlines())
    with open(os.path.join(configdir, "config"), "w") as f:
        f.write("\n".join(lines) + "\n")


class Site:
    def __init__(self, configdir: str):
        self.configdir = configdir
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + env.get("PYTHONPATH", "")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "site.py"), "--configdir", configdir],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        first = self._read()
        assert first.get("ready"), first
        self.transport_hash = first["transport_hash"]

    def _read(self) -> dict:
        line = self.proc.stdout.readline()
        if not line:
            err = self.proc.stderr.read()
            raise RuntimeError(f"site exited: {err[-4000:]}")
        return json.loads(line)

    def cmd(self, op: str, **kw) -> dict:
        self.proc.stdin.write(json.dumps({"op": op, **kw}) + "\n")
        self.proc.stdin.flush()
        reply = self._read()
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error"))
        return reply

    def wait_for(self, predicate, timeout: float = 20.0, interval: float = 0.25):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = predicate()
            if last:
                return last
            time.sleep(interval)
        return predicate()

    def peer_interfaces(self) -> list[dict]:
        return [i for i in self.cmd("status")["interfaces"] if i["type"] == "FIPSPeerInterface"]

    def online_peers(self) -> list[dict]:
        return [i for i in self.peer_interfaces() if i["online"]]

    def log(self) -> str:
        try:
            with open(os.path.join(self.configdir, "site.log")) as f:
                return f.read()
        except OSError:
            return ""

    def close(self):
        try:
            if self.proc.poll() is None:
                self.proc.stdin.write(json.dumps({"op": "exit"}) + "\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        finally:
            for s in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    s.close()
                except Exception:
                    pass
