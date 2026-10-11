"""The tunnel control socket must not hijack a live tunnel's socket, and must replace a stale file."""

import os
import socket
import threading

import pytest

from rnsfips.tunnel import _ControlServer, control_query, control_socket_alive


class _DummyTunnel:
    def __init__(self):
        self._stop = threading.Event()

    def control_command(self, command, params):
        return {"echo": command}


def test_stale_socket_file_is_replaced(tmp_path):
    path = str(tmp_path / "tunnel.sock")
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(path)
    dead.close()  # file stays behind, nobody listens: what a killed tunnel leaves
    assert os.path.exists(path) and not control_socket_alive(path)
    t = _DummyTunnel()
    srv = _ControlServer(t, path)
    srv.start()
    try:
        assert control_socket_alive(path)
        assert control_query("status", socket_path=path) == {"echo": "status"}
    finally:
        t._stop.set()
        srv.close()
    assert not os.path.exists(path)


def test_second_instance_refuses_live_socket(tmp_path):
    path = str(tmp_path / "tunnel.sock")
    t = _DummyTunnel()
    srv = _ControlServer(t, path)
    srv.start()
    try:
        with pytest.raises(RuntimeError, match="already serving"):
            _ControlServer(_DummyTunnel(), path)
        # The first instance is untouched.
        assert control_query("status", socket_path=path) == {"echo": "status"}
    finally:
        t._stop.set()
        srv.close()

