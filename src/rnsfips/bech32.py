"""Minimal bech32 (BIP-173) codec for Nostr npub keys.

npub1... encodes a 32-byte x-only secp256k1 public key with the human-readable
part ``npub``. Only what the bridge needs: decode an npub to its 32 bytes and
encode 32 bytes back into an npub.
"""

from __future__ import annotations

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_GEN = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)


def _polymod(values) -> int:
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            if (top >> i) & 1:
                chk ^= _GEN[i]
    return chk


def _hrp_expand(hrp: str):
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data, frombits: int, tobits: int, pad: bool):
    acc = 0
    bits = 0
    out = []
    maxv = (1 << tobits) - 1
    for value in data:
        if value < 0 or value >> frombits:
            raise ValueError("invalid value in bech32 data")
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if pad:
        if bits:
            out.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        raise ValueError("invalid bech32 padding")
    return out


def decode(text: str, expected_hrp: str = "npub") -> bytes:
    text = text.strip()
    if text != text.lower() and text != text.upper():
        raise ValueError("mixed-case bech32 string")
    text = text.lower()
    pos = text.rfind("1")
    if pos < 1 or pos + 7 > len(text) or len(text) > 90:
        raise ValueError("malformed bech32 string")
    hrp, data_part = text[:pos], text[pos + 1:]
    if hrp != expected_hrp:
        raise ValueError(f"expected hrp {expected_hrp!r}, got {hrp!r}")
    try:
        data = [CHARSET.index(c) for c in data_part]
    except ValueError:
        raise ValueError("invalid bech32 character") from None
    if _polymod(_hrp_expand(hrp) + data) != 1:
        raise ValueError("bad bech32 checksum")
    return bytes(_convertbits(data[:-6], 5, 8, False))


def encode(payload: bytes, hrp: str = "npub") -> str:
    data = _convertbits(payload, 8, 5, True)
    values = _hrp_expand(hrp) + data
    polymod = _polymod(values + [0] * 6) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(CHARSET[d] for d in data + checksum)


def npub_to_bytes(npub: str) -> bytes:
    key = decode(npub, "npub")
    if len(key) != 32:
        raise ValueError("npub does not hold a 32-byte key")
    return key


def bytes_to_npub(key: bytes) -> str:
    if len(key) != 32:
        raise ValueError("key must be 32 bytes")
    return encode(key, "npub")
