"""Two Reticulum sites bridged over the fake FIPS mesh: announces, paths, links, transfers."""

import pytest

from fake_fips import FakeFips, Mesh, fake_npub
from siteproc import Site, write_config

PORT = 4242
EXPECTED_HW_MTU = 1170 - 1


@pytest.fixture
def sites(tmp_path):
    mesh = Mesh()
    fips_a = FakeFips(str(tmp_path / "a.sock"), fake_npub("a"), mesh).start()
    fips_b = FakeFips(str(tmp_path / "b.sock"), fake_npub("b"), mesh).start()
    write_config(str(tmp_path / "rns-a"), fips_a.socket_path, PORT, [f"{fake_npub('b')}:{PORT}"])
    write_config(str(tmp_path / "rns-b"), fips_b.socket_path, PORT, [f"{fake_npub('a')}:{PORT}"])
    a = Site(str(tmp_path / "rns-a"))
    b = Site(str(tmp_path / "rns-b"))
    assert a.wait_for(a.online_peers)
    assert b.wait_for(b.online_peers)
    yield a, b, fips_a, fips_b
    a.close()
    b.close()
    fips_a.stop()
    fips_b.stop()


def test_announce_path_link_and_transfers(sites):
    a, b, fips_a, fips_b = sites
    # Both sides dialled each other; dedupe must leave exactly one flow per site.
    assert len(a.peer_interfaces()) == 1
    assert len(b.peer_interfaces()) == 1

    dest = b.cmd("announce", app="rnsfipstest", aspects=["echo"], app_data=b"hello-fips".hex())["hash"]
    assert a.wait_for(lambda: a.cmd("has_path", hash=dest)["has_path"], timeout=20)
    info = a.cmd("has_path", hash=dest)
    assert info["hops"] == 1
    assert info["next_hop_interface"].startswith("FIPSPeerInterface[FIPS/")
    assert a.cmd("recall_app_data", hash=dest)["app_data"] == b"hello-fips".hex()

    link = a.cmd("link", hash=dest, app="rnsfipstest", aspects=["echo"])
    assert link["mtu"] == EXPECTED_HW_MTU, link
    assert link["mdu"] > 500
    assert b.wait_for(lambda: b.cmd("received")["links"], timeout=10)

    pk = a.cmd("packet", link_id=link["link_id"], size=link["mdu"])
    assert pk["delivered"]
    assert b.wait_for(lambda: any(p["sha256"] == pk["sha256"] for p in b.cmd("received")["packets"]), timeout=10)

    res = a.cmd("resource", link_id=link["link_id"], size=200_000)
    assert res["complete"], res
    assert b.wait_for(lambda: any(r["sha256"] == res["sha256"] for r in b.cmd("received")["resources"]), timeout=20)

    # Announces propagate both ways with mode = full.
    dest_a = a.cmd("announce", app="rnsfipstest", aspects=["back"])["hash"]
    assert b.wait_for(lambda: b.cmd("has_path", hash=dest_a)["has_path"], timeout=20)
    assert fips_a.stats["dropped_oversize"] == 0 and fips_b.stats["dropped_oversize"] == 0
