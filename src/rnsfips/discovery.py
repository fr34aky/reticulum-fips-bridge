"""Bridge discovery over Reticulum announces.

Each ``FIPSInterface`` announces a Reticulum destination ``rnsfips.bridge``
whose ``app_data`` carries the node's FIPS npub, the FIPS port the interface
holds, and the other bridges it has learned about (gossip). A site that is
configured with one existing bridge learns the rest from that peer's announce
and, with ``auto_peer = yes``, dials them directly over FIPS.

Discovery needs Reticulum connectivity to exist first, so it never replaces
the first configured peer; it removes the need to configure every other one.

app_data is msgpack, kept well under Reticulum's ~317-byte announce budget:

    {"v": 1, "n": <32-byte x-only key>, "p": port, "t": <transport id hash>,
     "b": [[<32-byte key>, port], ...]}
"""

from __future__ import annotations

import json
import os
import time

from RNS.vendor import umsgpack

from . import bech32

APP_NAME = "rnsfips"
ASPECT = "bridge"
VERSION = 1
MAX_GOSSIP = 6
STATE_FILE = "rnsfips_bridges.json"


def encode_app_data(npub: str, port: int, transport_hash: bytes | None, known: dict) -> bytes:
    """``known`` maps npub -> {"port": int, "seen": float}; the freshest entries are gossiped."""
    entries = sorted(known.items(), key=lambda kv: kv[1].get("seen", 0), reverse=True)
    gossip = []
    for other, info in entries[:MAX_GOSSIP]:
        try:
            gossip.append([bech32.npub_to_bytes(other), int(info.get("port", port))])
        except ValueError:
            continue
    return umsgpack.packb({
        "v": VERSION, "n": bech32.npub_to_bytes(npub), "p": int(port),
        "t": transport_hash, "b": gossip,
    })


def decode_app_data(app_data: bytes) -> dict | None:
    """Return ``{"npub", "port", "tid", "bridges": [(npub, port), ...]}`` or None."""
    if not app_data:
        return None
    try:
        fields = umsgpack.unpackb(app_data)
    except Exception:
        return None
    if not isinstance(fields, dict) or fields.get("v") != VERSION:
        return None
    try:
        npub = bech32.bytes_to_npub(fields["n"])
        port = int(fields["p"])
    except (KeyError, ValueError, TypeError):
        return None
    if not 1024 <= port <= 65535:
        return None
    bridges = []
    for entry in fields.get("b") or []:
        try:
            other, oport = entry
            oport = int(oport)
            if 1024 <= oport <= 65535:
                bridges.append((bech32.bytes_to_npub(other), oport))
        except (ValueError, TypeError):
            continue
    tid = fields.get("t")
    return {"npub": npub, "port": port, "tid": tid if isinstance(tid, bytes) else None, "bridges": bridges}


def state_path(storagepath: str) -> str:
    return os.path.join(storagepath, STATE_FILE)


def save_state(storagepath: str, discovered: dict) -> None:
    """Persist the discovered bridges for ``rnsfips status`` (best effort)."""
    try:
        os.makedirs(storagepath, exist_ok=True)
        tmp = state_path(storagepath) + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"updated": time.time(), "bridges": discovered}, f, indent=1, default=str)
        os.replace(tmp, state_path(storagepath))
    except OSError:
        pass


def load_state(storagepath: str) -> dict:
    try:
        with open(state_path(storagepath)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}
