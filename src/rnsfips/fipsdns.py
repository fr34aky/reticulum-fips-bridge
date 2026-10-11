"""Talk to the FIPS daemon's built-in ``.fips`` DNS responder.

Resolving ``<npub>.fips`` is pure computation for the daemon, but it has a
useful side effect: the daemon registers the npub's public key in its
identity cache. FIPS builds before commit 55fb970 ("Deliver a native
datagram to an uncached, unrouted key after discovery") need that key to be
cached before a native-API flow to a node more than one hop away can
complete discovery. Priming through DNS is harmless on newer builds.

Only AAAA queries over UDP are needed, so this is a minimal encoder and no
DNS library is required.
"""

from __future__ import annotations

import os
import socket
import struct

DEFAULT_DNS = "[::1]:5354"


def parse_dns_addr(spec: str) -> tuple[str, int]:
    spec = spec.strip()
    if spec.startswith("["):
        host, _, port = spec[1:].partition("]")
        return host, int(port.lstrip(":") or 5354)
    if spec.count(":") == 1:
        host, port = spec.split(":")
        return host, int(port)
    return spec, 5354


def build_aaaa_query(name: str, txid: int | None = None) -> bytes:
    txid = int.from_bytes(os.urandom(2), "big") if txid is None else txid
    header = struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0)  # RD set
    qname = b"".join(bytes([len(l)]) + l.encode("ascii") for l in name.rstrip(".").split("."))
    return header + qname + b"\x00" + struct.pack("!HH", 28, 1)  # AAAA, IN


def parse_aaaa_answer(msg: bytes, txid: int) -> list[str]:
    """Return the AAAA addresses in a reply, or ``[]``. Raises on malformed data."""
    if len(msg) < 12:
        raise ValueError("short DNS reply")
    rid, flags, qd, an, _, _ = struct.unpack("!HHHHHH", msg[:12])
    if rid != txid:
        raise ValueError("DNS transaction id mismatch")
    if flags & 0x000F:
        return []
    pos = 12

    def skip_name(p):
        while True:
            l = msg[p]
            if l == 0:
                return p + 1
            if l & 0xC0 == 0xC0:
                return p + 2
            p += 1 + l

    for _ in range(qd):
        pos = skip_name(pos) + 4
    out = []
    for _ in range(an):
        pos = skip_name(pos)
        rtype, _, _, rdlen = struct.unpack("!HHIH", msg[pos:pos + 10])
        pos += 10
        rdata = msg[pos:pos + rdlen]
        pos += rdlen
        if rtype == 28 and rdlen == 16:
            out.append(socket.inet_ntop(socket.AF_INET6, rdata))
    return out


def resolve_npub(npub: str, dns_addr: str = DEFAULT_DNS, timeout: float = 2.0) -> str | None:
    """Ask the FIPS resolver for ``<npub>.fips``. Returns the IPv6 address or None."""
    host, port = parse_dns_addr(dns_addr)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    txid = int.from_bytes(os.urandom(2), "big")
    query = build_aaaa_query(f"{npub}.fips", txid)
    s = socket.socket(family, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        s.sendto(query, (host, port))
        reply, _ = s.recvfrom(1024)
    except OSError:
        return None
    finally:
        s.close()
    try:
        answers = parse_aaaa_answer(reply, txid)
    except (ValueError, IndexError, struct.error):
        return None
    return answers[0] if answers else None
