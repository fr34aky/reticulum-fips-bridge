import errno
import fcntl
import os
import socket

import pytest

from rnsfips.fipsapi import FipsApi, FipsApiError, FlowClosed

from fake_fips import FakeFips, Mesh, fake_npub, wait_for


@pytest.fixture
def mesh(tmp_path):
    mesh = Mesh()
    a = FakeFips(str(tmp_path / "a.sock"), fake_npub("a"), mesh).start()
    b = FakeFips(str(tmp_path / "b.sock"), fake_npub("b"), mesh).start()
    yield mesh, a, b
    a.stop()
    b.stop()


def test_listen_connect_roundtrip(mesh):
    _, a, b = mesh
    api_a = FipsApi(a.socket_path)
    api_b = FipsApi(b.socket_path)
    listener = api_b.listen(4242)
    assert listener.local_port == 4242
    assert listener.node == b.npub

    flow = api_a.connect(b.npub, 4242)
    assert flow.peer == b.npub
    assert flow.node == a.npub
    assert flow.max_payload == 1170
    flow.send(b"hello")

    inbound = listener.accept()
    assert inbound.peer == a.npub
    assert inbound.remote_port == flow.local_port
    assert inbound.held == 1
    assert inbound.recv() == b"hello"

    inbound.send(b"world")
    assert flow.recv() == b"world"

    # Descriptors are close-on-exec.
    assert fcntl.fcntl(flow.fileno(), fcntl.F_GETFD) & fcntl.FD_CLOEXEC
    assert fcntl.fcntl(listener.fileno(), fcntl.F_GETFD) & fcntl.FD_CLOEXEC

    flow.close()
    inbound.close()
    listener.close()


def test_oversize_send_raises_locally(mesh):
    _, a, b = mesh
    FipsApi(b.socket_path).listen(4243)
    flow = FipsApi(a.socket_path).connect(b.npub, 4243)
    with pytest.raises(OSError) as ei:
        flow.send(b"x" * (flow.max_payload + 1))
    assert ei.value.errno == errno.EMSGSIZE
    with pytest.raises(ValueError):
        flow.send(b"")
    flow.close()


def test_reserved_port_refused(mesh):
    _, a, _ = mesh
    with pytest.raises(FipsApiError) as ei:
        FipsApi(a.socket_path).listen(256)
    assert ei.value.errno_name == "EADDRNOTAVAIL"
    assert ei.value.errno == errno.EADDRNOTAVAIL


def test_duplicate_listen_refused(mesh):
    _, a, _ = mesh
    api = FipsApi(a.socket_path)
    l1 = api.listen(5000)
    with pytest.raises(FipsApiError) as ei:
        api.listen(5000)
    assert ei.value.errno_name == "EADDRINUSE"
    l1.close()


def test_daemon_restart_closes_flows(mesh):
    _, a, b = mesh
    listener = FipsApi(b.socket_path).listen(4244)
    flow = FipsApi(a.socket_path).connect(b.npub, 4244)
    flow.send(b"ping")
    inbound = listener.accept()
    assert inbound.recv() == b"ping"
    a.restart()
    with pytest.raises(FlowClosed):
        flow.recv()
    b.restart()
    with pytest.raises(FlowClosed):
        inbound.recv()
    with pytest.raises(FlowClosed):
        listener.accept()


def test_missing_socket(tmp_path):
    with pytest.raises(FipsApiError) as ei:
        FipsApi(str(tmp_path / "nope.sock")).listen(4242)
    assert ei.value.errno_name == "ECONNREFUSED"


def test_setup_timeout(tmp_path):
    path = str(tmp_path / "slow.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    s.listen(1)
    try:
        with pytest.raises(FipsApiError) as ei:
            FipsApi(path, timeout=0.3).listen(4242)
        assert ei.value.errno_name == "ETIMEDOUT"
    finally:
        s.close()
        os.unlink(path)


def test_unknown_peer_datagrams_are_dropped(mesh):
    _, a, _ = mesh
    flow = FipsApi(a.socket_path).connect(fake_npub("nobody"), 4242)
    flow.send(b"x")
    assert wait_for(lambda: a.stats["dropped_noroute"] >= 1)
    flow.close()
