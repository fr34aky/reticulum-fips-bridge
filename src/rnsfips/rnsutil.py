"""Helpers for talking to a running rnsd without starting a Reticulum instance."""

from __future__ import annotations

import socket
import sys
import time

SHARED_INSTANCE_TCP_PORT = 37428


def shared_instance_reachable(instance_name: str = "default", timeout: float = 1.0) -> bool:
    """Probe rnsd's shared-instance socket (abstract AF_UNIX on Linux, else TCP)."""
    if sys.platform.startswith("linux"):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(f"\0rns/{instance_name}")
            s.close()
            return True
        except OSError:
            pass
    try:
        s = socket.create_connection(("127.0.0.1", SHARED_INSTANCE_TCP_PORT), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


def wait_for_shared_instance(instance_name: str = "default", timeout: float | None = None,
                             interval: float = 2.0, on_wait=None) -> bool:
    """Block until the shared instance answers, or until ``timeout`` seconds (None = forever)."""
    deadline = None if timeout is None else time.monotonic() + timeout
    waited = False
    while not shared_instance_reachable(instance_name):
        if deadline is not None and time.monotonic() >= deadline:
            return False
        if on_wait and not waited:
            on_wait()
        waited = True
        time.sleep(interval)
    return True
