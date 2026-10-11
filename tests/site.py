#!/usr/bin/env python3
"""One Reticulum site for tests: a Reticulum instance driven over stdin/stdout.

Each line on stdin is a JSON command; each reply is one JSON line on stdout.
Reticulum logs go to <configdir>/site.log so stdout stays clean.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--configdir", required=True)
    args = ap.parse_args()
    configdir = args.configdir

    import RNS

    RNS.logfile = os.path.join(configdir, "site.log")
    reticulum = RNS.Reticulum(configdir=configdir, loglevel=RNS.LOG_DEBUG, logdest=RNS.LOG_FILE)

    lock = threading.Lock()
    destinations: dict[str, dict] = {}
    links: dict[str, RNS.Link] = {}
    received: dict[str, list] = {"packets": [], "resources": [], "links": []}
    state: dict = {}

    def reply(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def on_resource(resource):
        if resource.status == RNS.Resource.COMPLETE:
            data = resource.data.read()
            with lock:
                received["resources"].append({"size": len(data), "sha256": hashlib.sha256(data).hexdigest()})

    def on_packet(message, packet):
        with lock:
            received["packets"].append({"size": len(message), "sha256": hashlib.sha256(message).hexdigest()})

    def on_link(link):
        link.set_resource_strategy(RNS.Link.ACCEPT_ALL)
        link.set_resource_concluded_callback(on_resource)
        link.set_packet_callback(on_packet)
        with lock:
            received["links"].append(link.link_id.hex())

    def iface_info(i):
        return {
            "name": str(i), "cfg_name": getattr(i, "name", None), "type": type(i).__name__,
            "online": bool(i.online), "hw_mtu": i.HW_MTU, "mode": getattr(i, "mode", None),
            "rxb": i.rxb, "txb": i.txb, "ifac_size": getattr(i, "ifac_size", None),
            "parent": str(i.parent_interface) if i.parent_interface else None,
        }

    def handle(cmd):
        op = cmd["op"]
        if op == "status":
            try:
                reticulum.get_interface_stats()
                stats_ok = True
            except Exception as e:
                stats_ok = f"{type(e).__name__}: {e}"
            return {"interfaces": [iface_info(i) for i in RNS.Transport.interfaces], "stats_ok": stats_ok,
                    "transport_enabled": RNS.Reticulum.transport_enabled()}
        if op == "announce":
            app, aspects = cmd["app"], cmd.get("aspects", [])
            key = cmd.get("key", app)
            entry = destinations.get(key)
            if entry is None:
                identity = RNS.Identity()
                dest = RNS.Destination(identity, RNS.Destination.IN, RNS.Destination.SINGLE, app, *aspects)
                dest.set_proof_strategy(RNS.Destination.PROVE_ALL)
                dest.set_packet_callback(on_packet)
                dest.set_link_established_callback(on_link)
                entry = {"dest": dest, "identity": identity, "app": app, "aspects": aspects}
                destinations[key] = entry
            app_data = bytes.fromhex(cmd["app_data"]) if cmd.get("app_data") else None
            entry["dest"].announce(app_data=app_data)
            return {"hash": entry["dest"].hash.hex()}
        if op == "has_path":
            h = bytes.fromhex(cmd["hash"])
            return {"has_path": RNS.Transport.has_path(h), "hops": RNS.Transport.hops_to(h) if RNS.Transport.has_path(h) else None,
                    "next_hop_interface": str(RNS.Transport.next_hop_interface(h)) if RNS.Transport.has_path(h) else None}
        if op == "request_path":
            RNS.Transport.request_path(bytes.fromhex(cmd["hash"]))
            return {}
        if op == "recall_app_data":
            d = RNS.Identity.recall_app_data(bytes.fromhex(cmd["hash"]))
            return {"app_data": d.hex() if d else None}
        if op == "link":
            h = bytes.fromhex(cmd["hash"])
            identity = RNS.Identity.recall(h)
            if identity is None:
                raise RuntimeError("identity unknown; no announce seen")
            dest = RNS.Destination(identity, RNS.Destination.OUT, RNS.Destination.SINGLE, cmd["app"], *cmd.get("aspects", []))
            link = RNS.Link(dest)
            link.set_packet_callback(on_packet)
            deadline = time.time() + cmd.get("timeout", 20)
            while link.status not in (RNS.Link.ACTIVE, RNS.Link.CLOSED) and time.time() < deadline:
                time.sleep(0.05)
            if link.status != RNS.Link.ACTIVE:
                raise RuntimeError(f"link not established, status {link.status}")
            links[link.link_id.hex()] = link
            return {"link_id": link.link_id.hex(), "mtu": link.get_mtu(), "mdu": link.get_mdu(), "rtt": link.rtt}
        if op == "packet":
            link = links[cmd["link_id"]]
            data = os.urandom(cmd["size"])
            receipt = RNS.Packet(link, data).send()
            deadline = time.time() + cmd.get("timeout", 15)
            while receipt.status not in (RNS.PacketReceipt.DELIVERED, RNS.PacketReceipt.FAILED) and time.time() < deadline:
                time.sleep(0.05)
            return {"delivered": receipt.status == RNS.PacketReceipt.DELIVERED, "sha256": hashlib.sha256(data).hexdigest()}
        if op == "resource":
            link = links[cmd["link_id"]]
            data = os.urandom(cmd["size"])
            done = threading.Event()
            resource = RNS.Resource(data, link, callback=lambda r: done.set())
            started = time.time()
            done.wait(cmd.get("timeout", 120))
            return {"status": resource.status, "complete": resource.status == RNS.Resource.COMPLETE,
                    "seconds": time.time() - started, "sha256": hashlib.sha256(data).hexdigest()}
        if op == "received":
            with lock:
                return {k: list(v) for k, v in received.items()}
        if op == "detach":
            reticulum.detach_interface(cmd["name"])
            return {}
        if op == "attach":
            reticulum.attach_interface(cmd["name"])
            return {}
        if op == "tunnel_start":
            from rnsfips.tunnel import Tunnel, TunnelConfig
            t = Tunnel(TunnelConfig(**cmd["config"]), reticulum).start()
            state["tunnel"] = t
            return {"dest": t.destination.hash.hex(), "npub": t.npub}
        if op == "tunnel_status":
            return state["tunnel"].status()
        if op == "tunnel_announce":
            state["tunnel"].announce()
            return {}
        if op == "tunnel_stop":
            state["tunnel"].stop()
            return {}
        if op == "exit":
            return {"bye": True}
        raise ValueError(f"unknown op {op}")

    reply({"ready": True, "transport_hash": RNS.Transport.identity.hash.hex()})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
            result = handle(cmd)
            reply({"ok": True, **result})
            if cmd.get("op") == "exit":
                break
        except Exception as e:
            reply({"ok": False, "error": f"{type(e).__name__}: {e}"})
    try:
        RNS.Reticulum.exit_handler()
    finally:
        os._exit(0)


if __name__ == "__main__":
    main()
