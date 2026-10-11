"""Bridge envelope shared by both bridge directions.

Every datagram the bridge puts on a FIPS flow or a Reticulum link starts
with one type byte. A datagram is therefore never empty, which is what lets
a zero-length read on a FIPS flow mean "the daemon closed it".

    0x01 HELLO       msgpack map, see :func:`encode_hello`
    0x02 PING        empty
    0x03 PONG        empty
    0x10 RNS_FRAME   one complete Reticulum frame (IFAC already applied)
    0x20 FIPS_FRAME  one complete FIPS wire frame
    0x21 FIPS_FRAG   fragment header + part of a FIPS wire frame

Fragment header (after the type byte): ``msg_id u16 BE | index u8 | count u8``.
Fragments are only used in the FIPS-over-Reticulum direction, where a FIPS
frame can be larger than a Reticulum link MDU. Reassembly is best effort,
matching FIPS's own loss model.
"""

from __future__ import annotations

import struct
import time

from RNS.vendor import umsgpack

VERSION = 1

HELLO = 0x01
PING = 0x02
PONG = 0x03
RNS_FRAME = 0x10
FIPS_FRAME = 0x20
FIPS_FRAG = 0x21

TYPE_NAMES = {
    HELLO: "HELLO",
    PING: "PING",
    PONG: "PONG",
    RNS_FRAME: "RNS_FRAME",
    FIPS_FRAME: "FIPS_FRAME",
    FIPS_FRAG: "FIPS_FRAG",
}

_FRAG_HDR = struct.Struct("!HBB")
FRAG_OVERHEAD = 1 + _FRAG_HDR.size


class EnvelopeError(ValueError):
    pass


def encode_hello(fields: dict) -> bytes:
    body = dict(fields)
    body.setdefault("v", VERSION)
    return bytes([HELLO]) + umsgpack.packb(body)


def decode_hello(payload: bytes) -> dict:
    try:
        fields = umsgpack.unpackb(payload)
    except Exception as e:
        raise EnvelopeError(f"malformed HELLO: {e}") from e
    if not isinstance(fields, dict):
        raise EnvelopeError("malformed HELLO: not a map")
    if fields.get("v") != VERSION:
        raise EnvelopeError(f"unsupported bridge version {fields.get('v')!r}")
    return fields


def ping() -> bytes:
    return bytes([PING])


def pong() -> bytes:
    return bytes([PONG])


def rns_frame(frame: bytes) -> bytes:
    return bytes([RNS_FRAME]) + frame


def split(datagram: bytes):
    """Return ``(type, payload)`` for a received datagram."""
    if not datagram:
        raise EnvelopeError("empty datagram")
    return datagram[0], datagram[1:]


class Fragmenter:
    """Split FIPS frames into datagrams that fit a given payload size."""

    def __init__(self):
        self._next_id = 0

    def encode(self, frame: bytes, max_payload: int) -> list[bytes]:
        if max_payload <= FRAG_OVERHEAD:
            raise EnvelopeError(f"max_payload {max_payload} too small to fragment")
        if len(frame) + 1 <= max_payload:
            return [bytes([FIPS_FRAME]) + frame]
        chunk = max_payload - FRAG_OVERHEAD
        count = (len(frame) + chunk - 1) // chunk
        if count > 255:
            raise EnvelopeError(f"frame of {len(frame)} bytes needs {count} fragments")
        msg_id = self._next_id
        self._next_id = (self._next_id + 1) & 0xFFFF
        out = []
        for index in range(count):
            part = frame[index * chunk:(index + 1) * chunk]
            out.append(bytes([FIPS_FRAG]) + _FRAG_HDR.pack(msg_id, index, count) + part)
        return out


class Reassembler:
    """Collect fragments into frames. Incomplete frames expire after ``timeout``."""

    def __init__(self, timeout: float = 5.0, max_pending: int = 256):
        self.timeout = timeout
        self.max_pending = max_pending
        self._pending: dict[int, tuple[float, int, dict[int, bytes]]] = {}

    def feed(self, payload: bytes, now: float | None = None) -> bytes | None:
        """Feed the payload of a FIPS_FRAG datagram. Return the frame when complete."""
        if len(payload) < _FRAG_HDR.size:
            raise EnvelopeError("short fragment")
        msg_id, index, count = _FRAG_HDR.unpack_from(payload)
        if count == 0 or index >= count:
            raise EnvelopeError("bad fragment indices")
        now = time.monotonic() if now is None else now
        self.expire(now)
        entry = self._pending.get(msg_id)
        if entry is None or entry[1] != count:
            if len(self._pending) >= self.max_pending:
                oldest = min(self._pending, key=lambda k: self._pending[k][0])
                del self._pending[oldest]
            entry = (now, count, {})
            self._pending[msg_id] = entry
        entry[2][index] = payload[_FRAG_HDR.size:]
        if len(entry[2]) == count:
            del self._pending[msg_id]
            return b"".join(entry[2][i] for i in range(count))
        return None

    def expire(self, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        dead = [k for k, (t, _, _) in self._pending.items() if now - t > self.timeout]
        for k in dead:
            del self._pending[k]
        return len(dead)

    @property
    def pending(self) -> int:
        return len(self._pending)
