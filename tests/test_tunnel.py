"""FIPS over Reticulum: two tunnels linked across a Reticulum TCP hop, fake FIPS on each side."""

import os
import socket
import time

import pytest

from fake_fips import FakeControl, fake_npub, wait_for
from siteproc import Site, write_config


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def rig(tmp_path):
    tcp_port = free_port()
    write_config(str(tmp_path / "rns-a"), None, 0, [], extra_interfaces=f"""
        [[TCP]]
          type = TCPServerInterface
          enabled = yes
          listen_ip = 127.0.0.1
          listen_port = {tcp_port}
    """)
    write_config(str(tmp_path / "rns-b"), None, 0, [], extra_interfaces=f"""
        [[TCP]]
          type = TCPClientInterface
          enabled = yes
          target_host = 127.0.0.1
          target_port = {tcp_port}
    """)
    a = Site(str(tmp_path / "rns-a"))
    b = Site(str(tmp_path / "rns-b"))
    ctl_a = FakeControl(str(tmp_path / "ctl-a.sock"), fake_npub("fips-a")).start()
    ctl_b = FakeControl(str(tmp_path / "ctl-b.sock"), fake_npub("fips-b")).start()
    udp_a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_a.bind(("127.0.0.1", 0))
    udp_b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_b.bind(("127.0.0.1", 0))
    udp_a.settimeout(10)
    udp_b.settimeout(10)
    yield dict(a=a, b=b, ctl_a=ctl_a, ctl_b=ctl_b, udp_a=udp_a, udp_b=udp_b)
    a.close()
    b.close()
    ctl_a.stop()
    ctl_b.stop()
    udp_a.close()
    udp_b.close()


def start_tunnel(site, ctl, udp, **extra):
    cfg = {
        "control_socket": os.path.join(os.path.dirname(ctl.socket_path), f"t-{os.path.basename(ctl.socket_path)}"),
        "fips_control": ctl.socket_path,
        "fips_udp_addr": "127.0.0.1:%d" % udp.getsockname()[1],
        "allow": "any",
        "reconnect_wait": 1,
        "connect_refresh": 2,
        "announce_interval": 3600,
    }
    cfg.update(extra)
    return site.cmd("tunnel_start", config=cfg)


@pytest.mark.parametrize("mdu_override", [0, 400])
def test_fips_frames_cross_the_tunnel(rig, mdu_override):
    a, b = rig["a"], rig["b"]
    tb = start_tunnel(b, rig["ctl_b"], rig["udp_b"], mdu_override=mdu_override)
    ta = start_tunnel(a, rig["ctl_a"], rig["udp_a"], peers=tb["dest"], mdu_override=mdu_override)
    assert ta["npub"] == fake_npub("fips-a")

    def a_up():
        st = a.cmd("tunnel_status")
        return [p for p in st["peers"] if p["up"] and p["npub"] == fake_npub("fips-b")]

    def b_up():
        st = b.cmd("tunnel_status")
        return [p for p in st["peers"] if p["up"] and p["npub"] == fake_npub("fips-a")]

    assert a.wait_for(a_up, timeout=40), a.log()[-3000:]
    assert b.wait_for(b_up, timeout=40), b.log()[-3000:]
    port_a = a_up()[0]["udp_port"]
    port_b = b_up()[0]["udp_port"]
    if mdu_override == 0:
        assert a_up()[0]["mdu"] > 1300  # TCP hop: link MTU discovery gives room for whole FIPS frames

    # Each tunnel told its FIPS daemon to peer with the remote npub at the shim address.
    assert wait_for(lambda: any(c["npub"] == fake_npub("fips-b") and c["address"] == f"127.0.0.1:{port_a}"
                                for c in rig["ctl_a"].connects()))
    assert wait_for(lambda: any(c["npub"] == fake_npub("fips-a") and c["address"] == f"127.0.0.1:{port_b}"
                                for c in rig["ctl_b"].connects()))

    # A FIPS-sized frame from the fake daemon on A arrives at the fake daemon on B, from B's shim port.
    frame = bytes(range(256)) * 5  # 1280 bytes, the FIPS default MTU
    rig["udp_a"].sendto(frame, ("127.0.0.1", port_a))
    data, src = rig["udp_b"].recvfrom(65535)
    assert data == frame
    assert src == ("127.0.0.1", port_b)

    # And back, small and large.
    rig["udp_b"].sendto(b"pong", ("127.0.0.1", port_b))
    data, src = rig["udp_a"].recvfrom(65535)
    assert data == b"pong" and src == ("127.0.0.1", port_a)
    rig["udp_b"].sendto(frame[::-1], ("127.0.0.1", port_b))
    data, _ = rig["udp_a"].recvfrom(65535)
    assert data == frame[::-1]

    # Counters are updated after the datagram is written, so wait for them.
    assert a.wait_for(lambda: (lambda p: p["tx_frames"] >= 1 and p["rx_frames"] >= 2)(a.cmd("tunnel_status")["peers"][0]), timeout=5)

    # Announces carry the npub binding; a re-announce from B is seen by A's handler.
    # (Reticulum ignores an announce emitted in the same second as the last one, so retry.)
    import time
    for _ in range(5):
        time.sleep(1.1)
        b.cmd("tunnel_announce")
        if a.wait_for(lambda: tb["dest"] in a.cmd("tunnel_status")["discovered"], timeout=3):
            break
    assert tb["dest"] in a.cmd("tunnel_status")["discovered"]
    assert a.cmd("tunnel_status")["discovered"][tb["dest"]]["npub"] == fake_npub("fips-b")

    # The tunnel control socket answers the same status, and can add/remove peers.
    from rnsfips.tunnel import control_query
    sock_a = os.path.join(os.path.dirname(rig["ctl_a"].socket_path), "t-ctl-a.sock")
    st = control_query("status", socket_path=sock_a)
    assert st["npub"] == fake_npub("fips-a") and st["peers"][0]["up"]
    assert control_query("add_peer", {"dest": "00" * 16}, socket_path=sock_a) == {"changed": True}
    assert control_query("remove_peer", {"dest": "00" * 16}, socket_path=sock_a) == {"changed": True}
    assert control_query("remove_peer", {"dest": "00" * 16}, socket_path=sock_a) == {"changed": False}
    with pytest.raises(RuntimeError):
        control_query("bogus", socket_path=sock_a)
    # A tunnel's own destination hash is a configuration mistake, not a peer.
    with pytest.raises(RuntimeError, match="own destination"):
        control_query("add_peer", {"dest": st["destination"]}, socket_path=sock_a)


def test_inbound_link_refused_when_not_allowed(rig):
    a, b = rig["a"], rig["b"]
    tb = start_tunnel(b, rig["ctl_b"], rig["udp_b"], allow="0" * 32)
    start_tunnel(a, rig["ctl_a"], rig["udp_a"], peers=tb["dest"])
    import time
    # Sample repeatedly: a refused initiator must never be told the tunnel is up,
    # and neither side may ask its FIPS daemon to dial.
    for _ in range(12):
        time.sleep(1)
        assert not any(p["up"] for p in a.cmd("tunnel_status")["peers"])
        assert not any(p["up"] for p in b.cmd("tunnel_status")["peers"])
    assert not rig["ctl_a"].connects()
    assert not rig["ctl_b"].connects()



def _one_link_each(a, b, up):
    """Exactly one up link per side, with consistent roles, that is not redialled."""
    assert a.wait_for(lambda: len(up(a, fake_npub("fips-b"))) == 1 and len(up(b, fake_npub("fips-a"))) == 1, timeout=30), \
        a.log()[-2000:] + b.log()[-2000:]
    time.sleep(6)  # longer than reconnect_wait: the loser must not be redialled
    ra, rb = up(a, fake_npub("fips-b")), up(b, fake_npub("fips-a"))
    assert len(ra) == 1 and len(rb) == 1
    assert ra[0]["initiator"] != rb[0]["initiator"]
    return ra[0], rb[0]


def _up(site, npub):
    return [p for p in site.cmd("tunnel_status")["peers"] if p["up"] and p["npub"] == npub]


def test_mutual_dial_keeps_one_link(rig):
    """B is added as a peer of A first; A then adds B. The second dial is never made."""
    a, b = rig["a"], rig["b"]
    tb = start_tunnel(b, rig["ctl_b"], rig["udp_b"])
    ta = start_tunnel(a, rig["ctl_a"], rig["udp_a"], peers=tb["dest"])
    assert a.wait_for(lambda: _up(a, fake_npub("fips-b")), timeout=40), a.log()[-3000:]
    assert b.cmd("tunnel_ctl", command="add_peer", params={"dest": ta["dest"]})["changed"] is True
    ra, rb = _one_link_each(a, b, _up)
    assert ra["initiator"] and not rb["initiator"]  # A's existing link stands; B never dialled
    rig["udp_a"].sendto(b"\x55" * 600, ("127.0.0.1", ra["udp_port"]))
    data, _ = rig["udp_b"].recvfrom(2048)
    assert data == b"\x55" * 600


def test_simultaneous_dial_keeps_one_link(rig):
    """Both sides dial at once: the link initiated by the smaller destination hash survives."""
    a, b = rig["a"], rig["b"]
    tb = start_tunnel(b, rig["ctl_b"], rig["udp_b"])
    ta = start_tunnel(a, rig["ctl_a"], rig["udp_a"])
    # Prime paths so both dials go out on the next supervisor tick.
    a.cmd("tunnel_announce"); b.cmd("tunnel_announce")
    time.sleep(2)
    a.cmd("tunnel_ctl", command="add_peer", params={"dest": tb["dest"]})
    b.cmd("tunnel_ctl", command="add_peer", params={"dest": ta["dest"]})
    ra, rb = _one_link_each(a, b, _up)
    if "duplicate" in a.log() + b.log():  # both dials raced: the deterministic rule must have decided
        smaller_is_a = bytes.fromhex(ta["dest"]) < bytes.fromhex(tb["dest"])
        assert ra["initiator"] == smaller_is_a and rb["initiator"] == (not smaller_is_a)
    rig["udp_b"].sendto(b"\x66" * 600, ("127.0.0.1", rb["udp_port"]))
    data, _ = rig["udp_a"].recvfrom(2048)
    assert data == b"\x66" * 600
