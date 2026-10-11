"""FIPSInterface inside a real Reticulum instance, against the fake FIPS daemon."""

import os
import time

import pytest

from fake_fips import FakeFips, Mesh, fake_npub, wait_for
from siteproc import Site, write_config

PORT = 4242


@pytest.fixture
def mesh():
    m = Mesh()
    yield m
    for n in list(m.nodes.values()):
        n.stop()


def make_site(tmp_path, mesh, label, peers, **kw):
    fips = FakeFips(str(tmp_path / f"{label}.sock"), fake_npub(label), mesh).start()
    cfg = tmp_path / f"rns-{label}"
    write_config(str(cfg), fips.socket_path, PORT, peers, **kw)
    return fips, Site(str(cfg))


def test_parent_registers_and_peer_comes_up(tmp_path, mesh):
    fips_a, site_a = make_site(tmp_path, mesh, "a", [f"{fake_npub('b')}:{PORT}"])
    fips_b, site_b = make_site(tmp_path, mesh, "b", [], allow=[fake_npub("a")])
    try:
        st = site_a.cmd("status")
        parents = [i for i in st["interfaces"] if i["type"] == "FIPSInterface"]
        assert parents and parents[0]["online"] and parents[0]["name"] == "FIPSInterface[FIPS]"
        assert st["stats_ok"] is True

        assert site_a.wait_for(site_a.online_peers)
        assert site_b.wait_for(site_b.online_peers)
        pa = site_a.online_peers()[0]
        pb = site_b.online_peers()[0]
        assert pa["name"] == f"FIPSPeerInterface[FIPS/{fake_npub('b')}]"
        assert pb["name"] == f"FIPSPeerInterface[FIPS/{fake_npub('a')}]"
        assert pa["hw_mtu"] == 1170 - 1  # no IFAC configured
        assert pa["mode"] == pb["mode"]
        assert pa["parent"] == "FIPSInterface[FIPS]"
        assert site_a.cmd("status")["stats_ok"] is True
        assert site_b.cmd("status")["stats_ok"] is True
        # Only one flow per direction after dedupe.
        assert len(site_a.peer_interfaces()) == 1
        assert len(site_b.peer_interfaces()) == 1
    finally:
        site_a.close()
        site_b.close()


def test_unlisted_peer_is_refused(tmp_path, mesh):
    fips_a, site_a = make_site(tmp_path, mesh, "a", [f"{fake_npub('b')}:{PORT}"])
    fips_b, site_b = make_site(tmp_path, mesh, "b", [])
    try:
        time.sleep(3)
        assert site_b.peer_interfaces() == []
        assert site_a.online_peers() == []
    finally:
        site_a.close()
        site_b.close()


def test_daemon_restart_reconnects_configured_peer(tmp_path, mesh):
    fips_a, site_a = make_site(tmp_path, mesh, "a", [f"{fake_npub('b')}:{PORT}"])
    fips_b, site_b = make_site(tmp_path, mesh, "b", [], allow=[fake_npub("a")])
    try:
        assert site_a.wait_for(site_a.online_peers)
        assert site_b.wait_for(site_b.online_peers)
        fips_a.restart()
        # Site A keeps the configured peer registered but offline, then redials.
        assert site_a.wait_for(lambda: site_a.peer_interfaces() and not site_a.online_peers(), timeout=10)
        assert site_a.wait_for(site_a.online_peers, timeout=15)
        # Site B's inbound-spawned peer is torn down and replaced by a fresh one.
        assert site_b.wait_for(lambda: len(site_b.peer_interfaces()) == 1 and site_b.online_peers(), timeout=15)
        assert site_a.cmd("status")["stats_ok"] is True
    finally:
        site_a.close()
        site_b.close()


def test_daemon_starts_late(tmp_path, mesh):
    sock = str(tmp_path / "late.sock")
    cfg = tmp_path / "rns-late"
    write_config(str(cfg), sock, PORT, [f"{fake_npub('b')}:{PORT}"])
    site = Site(str(cfg))
    fips_b, site_b = make_site(tmp_path, mesh, "b", [], allow=[fake_npub("late")])
    try:
        time.sleep(2)
        assert site.online_peers() == []
        parent = [i for i in site.cmd("status")["interfaces"] if i["type"] == "FIPSInterface"][0]
        assert parent["online"]
        fips = FakeFips(sock, fake_npub("late"), mesh).start()
        assert site.wait_for(site.online_peers, timeout=15)
        assert site_b.wait_for(site_b.online_peers, timeout=15)
    finally:
        site.close()
        site_b.close()


def test_detach_and_attach(tmp_path, mesh):
    fips_a, site_a = make_site(tmp_path, mesh, "a", [f"{fake_npub('b')}:{PORT}"])
    fips_b, site_b = make_site(tmp_path, mesh, "b", [], allow=[fake_npub("a")])
    try:
        assert site_a.wait_for(site_a.online_peers)
        site_a.cmd("detach", name="FIPS")
        assert site_a.wait_for(lambda: site_a.cmd("status")["interfaces"] == [], timeout=10)
        assert wait_for(lambda: not fips_a.flows and not fips_a.listeners, timeout=5)
        assert site_b.wait_for(lambda: site_b.online_peers() == [], timeout=15)
        site_a.cmd("attach", name="FIPS")
        assert site_a.wait_for(site_a.online_peers, timeout=15)
        assert site_b.wait_for(site_b.online_peers, timeout=15)
        assert site_a.cmd("status")["stats_ok"] is True
    finally:
        site_a.close()
        site_b.close()


def test_peer_rnsd_restart_replaces_dead_flow(tmp_path, mesh):
    """When B's rnsd restarts and redials, A must switch to the new flow at once (HELLO nonce
    differs), not wait for the old flow to time out. peer_timeout is set long to prove it."""
    fips_a, site_a = make_site(tmp_path, mesh, "a", [f"{fake_npub('b')}:{PORT}"], extra="peer_timeout = 300")
    fips_b, site_b = make_site(tmp_path, mesh, "b", [f"{fake_npub('a')}:{PORT}"], extra="peer_timeout = 300")
    try:
        assert site_a.wait_for(site_a.online_peers)
        assert site_b.wait_for(site_b.online_peers)
        dest = site_b.cmd("announce", app="rnsfipstest", aspects=["x"])["hash"]
        assert site_a.wait_for(lambda: site_a.cmd("has_path", hash=dest)["has_path"], timeout=20)

        site_b.close()  # B's rnsd dies; A's flow to it is now dead but A cannot know.
        site_b2 = Site(str(tmp_path / "rns-b"))
        assert site_b2.wait_for(site_b2.online_peers, timeout=40), site_b2.log()[-3000:]
        # A must be up on the NEW flow well within peer_timeout.
        assert site_a.wait_for(site_a.online_peers, timeout=20)
        t0 = time.time()
        dest2 = site_b2.cmd("announce", app="rnsfipstest", aspects=["y"])["hash"]
        assert site_a.wait_for(lambda: site_a.cmd("has_path", hash=dest2)["has_path"], timeout=30)
        assert time.time() - t0 < 30
        assert len(site_a.peer_interfaces()) == 1
        site_b2.close()
    finally:
        site_a.close()
