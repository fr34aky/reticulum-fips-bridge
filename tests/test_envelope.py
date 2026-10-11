import pytest

from rnsfips import envelope as env


def test_hello_roundtrip():
    dg = env.encode_hello({"impl": "rnsfips", "nonce": b"\x01" * 8, "hw_mtu": 1153})
    kind, payload = env.split(dg)
    assert kind == env.HELLO
    fields = env.decode_hello(payload)
    assert fields["v"] == env.VERSION
    assert fields["hw_mtu"] == 1153
    assert fields["nonce"] == b"\x01" * 8


def test_hello_rejects_bad_version():
    from RNS.vendor import umsgpack

    with pytest.raises(env.EnvelopeError):
        env.decode_hello(umsgpack.packb({"v": 99}))
    with pytest.raises(env.EnvelopeError):
        env.decode_hello(b"\xff\xff")


def test_split_empty():
    with pytest.raises(env.EnvelopeError):
        env.split(b"")


def test_fragment_small_frame_is_not_fragmented():
    f = env.Fragmenter()
    out = f.encode(b"abc", 100)
    assert out == [bytes([env.FIPS_FRAME]) + b"abc"]


def test_fragment_and_reassemble():
    f = env.Fragmenter()
    r = env.Reassembler()
    frame = bytes(range(256)) * 5  # 1280 bytes
    parts = f.encode(frame, 431)
    assert len(parts) == 4
    assert all(len(p) <= 431 for p in parts)
    assert all(p[0] == env.FIPS_FRAG for p in parts)
    # Deliver out of order.
    order = [2, 0, 3, 1]
    got = None
    for i in order:
        res = r.feed(parts[i][1:])
        if res is not None:
            got = res
    assert got == frame
    assert r.pending == 0


def test_reassembler_expires():
    f = env.Fragmenter()
    r = env.Reassembler(timeout=1.0)
    parts = f.encode(b"x" * 2000, 500)
    assert r.feed(parts[0][1:], now=0.0) is None
    assert r.pending == 1
    assert r.expire(now=2.0) == 1
    assert r.pending == 0


def test_fragment_too_many():
    f = env.Fragmenter()
    with pytest.raises(env.EnvelopeError):
        f.encode(b"x" * 300_000, 100)
