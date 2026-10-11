import socket
import struct
import threading

from rnsfips import fipsdns


def test_query_encoding_and_answer_parsing():
    q = fipsdns.build_aaaa_query("npub1abc.fips", txid=0x1234)
    assert q[:2] == b"\x12\x34"
    assert q.endswith(b"\x08npub1abc\x04fips\x00\x00\x1c\x00\x01")
    # Build a reply: same question, one AAAA answer using a name pointer.
    reply = struct.pack("!HHHHHH", 0x1234, 0x8180, 1, 1, 0, 0) + q[12:]
    reply += b"\xc0\x0c" + struct.pack("!HHIH", 28, 1, 300, 16) + socket.inet_pton(socket.AF_INET6, "fd00::1")
    assert fipsdns.parse_aaaa_answer(reply, 0x1234) == ["fd00::1"]
    # NXDOMAIN yields no answers.
    nx = struct.pack("!HHHHHH", 0x1234, 0x8183, 1, 0, 0, 0) + q[12:]
    assert fipsdns.parse_aaaa_answer(nx, 0x1234) == []


def test_resolve_against_fake_responder():
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]

    def serve():
        data, addr = srv.recvfrom(1024)
        txid = data[:2]
        reply = txid + struct.pack("!HHHHH", 0x8180, 1, 1, 0, 0) + data[12:]
        reply += b"\xc0\x0c" + struct.pack("!HHIH", 28, 1, 300, 16) + socket.inet_pton(socket.AF_INET6, "fd05::2")
        srv.sendto(reply, addr)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    assert fipsdns.resolve_npub("npub1xyz", f"127.0.0.1:{port}") == "fd05::2"
    srv.close()


def test_resolve_timeout_returns_none():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    assert fipsdns.resolve_npub("npub1xyz", f"127.0.0.1:{port}", timeout=0.2) is None
    s.close()


def test_parse_dns_addr():
    assert fipsdns.parse_dns_addr("[::1]:5354") == ("::1", 5354)
    assert fipsdns.parse_dns_addr("127.0.0.1:53") == ("127.0.0.1", 53)
    assert fipsdns.parse_dns_addr("[fd00::1]") == ("fd00::1", 5354)
