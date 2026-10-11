"""Bridge discovery: a third site configured with one bridge learns and dials the other."""

import pytest

from fake_fips import FakeFips, Mesh, fake_npub
from rnsfips import discovery
from siteproc import Site, write_config

PORT = 4242


def test_app_data_roundtrip():
    known = {fake_npub("x"): {"port": 5000, "seen": 2}, fake_npub("y"): {"port": 5001, "seen": 1}}
    data = discovery.encode_app_data(fake_npub("me"), 4242, b"\x01" * 16, known)
    assert len(data) < 317
    info = discovery.decode_app_data(data)
    assert info["npub"] == fake_npub("me") and info["port"] == 4242 and info["tid"] == b"\x01" * 16
    assert info["bridges"] == [(fake_npub("x"), 5000), (fake_npub("y"), 5001)]
    assert discovery.decode_app_data(b"") is None
    assert discovery.decode_app_data(b"\xc0") is None
    # Ten known bridges still fit the announce budget (gossip is capped).
    many = {fake_npub(str(i)): {"port": 4242, "seen": i} for i in range(10)}
    assert len(discovery.encode_app_data(fake_npub("me"), 4242, b"\x02" * 16, many)) < 317


def test_third_site_learns_and_dials(tmp_path):
    mesh = Mesh()
    fips = {}
    sites = {}
    for label, peers in (("a", []), ("b", [f"{fake_npub('a')}:{PORT}"])):
        fips[label] = FakeFips(str(tmp_path / f"{label}.sock"), fake_npub(label), mesh).start()
        write_config(str(tmp_path / f"rns-{label}"), fips[label].socket_path, PORT, peers,
                     accept_unknown=True, extra="auto_peer = yes\nannounce_interval = 20")
        sites[label] = Site(str(tmp_path / f"rns-{label}"))
    try:
        assert sites["a"].wait_for(sites["a"].online_peers, timeout=30)
        assert sites["b"].wait_for(sites["b"].online_peers, timeout=30)

        # Site C knows only A. It must learn B from A's gossip and dial B directly.
        fips["c"] = FakeFips(str(tmp_path / "c.sock"), fake_npub("c"), mesh).start()
        write_config(str(tmp_path / "rns-c"), fips["c"].socket_path, PORT, [f"{fake_npub('a')}:{PORT}"],
                     accept_unknown=True, extra="auto_peer = yes\nannounce_interval = 20")
        sites["c"] = Site(str(tmp_path / "rns-c"))

        def c_has_b():
            return [i for i in sites["c"].online_peers() if fake_npub("b") in i["name"]]

        def b_has_c():
            return [i for i in sites["b"].online_peers() if fake_npub("c") in i["name"]]

        assert sites["c"].wait_for(c_has_b, timeout=120), sites["c"].log()[-4000:]
        assert sites["b"].wait_for(b_has_c, timeout=60), sites["b"].log()[-4000:]
        # Exactly one flow per pair after dedupe.
        assert len([i for i in sites["c"].peer_interfaces() if fake_npub("b") in i["name"]]) == 1
        state = discovery.load_state(str(tmp_path / "rns-c" / "storage"))
        assert fake_npub("b") in state.get("bridges", {})
    finally:
        for s in sites.values():
            s.close()
        for f in fips.values():
            f.stop()
