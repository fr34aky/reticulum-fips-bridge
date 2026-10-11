"""Command line entry point: ``rnsfips``."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

from . import __version__

SAMPLE_CONFIG = """\
  [[FIPS]]
    type = FIPSInterface
    enabled = yes
    mode = full
    # FIPS native API socket; auto-detected when omitted
    # fips_socket = /run/fips/api.sock
    # FIPS application port this interface holds and dials (1024-65535)
    listen_port = 21075
    # FIPS peers (npubs) running the same interface, comma separated
    peers = npub1...
    # Extra npubs allowed to connect inbound; accept_unknown = yes allows any
    # allow = npub1...
"""


def _shim_source() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "FIPSInterface.py")
    if not os.path.exists(path):
        raise FileNotFoundError(f"FIPSInterface.py shim not found at {path}")
    return path


def cmd_install_interface(args) -> int:
    configdir = os.path.expanduser(args.configdir)
    ifdir = os.path.join(configdir, "interfaces")
    os.makedirs(ifdir, exist_ok=True)
    dest = os.path.join(ifdir, "FIPSInterface.py")
    shutil.copyfile(_shim_source(), dest)
    print(f"installed {dest}")
    print("add this to the [interfaces] section of", os.path.join(configdir, "config"), ":\n")
    print(SAMPLE_CONFIG)
    return 0


def cmd_status(args) -> int:
    from .fipsctl import FipsControl, FipsControlError

    ctl = FipsControl(args.control_socket)
    try:
        status = ctl.show_status()
        flows = ctl.show_native_flows()
    except FipsControlError as e:
        print(f"FIPS control socket: {e}", file=sys.stderr)
        return 1
    print(f"FIPS node {status.get('npub')} state={status.get('state')} peers={status.get('peer_count')} "
          f"sessions={status.get('session_count')} ipv6_mtu={status.get('effective_ipv6_mtu')}")
    listeners = flows.get("listeners", [])
    print(f"native API listeners: {[l.get('local_port') for l in listeners]}")
    for f in flows.get("flows", []):
        print(f"  flow {f.get('flow_id')} {f.get('peer', '')[:20]}… {f.get('local_port')}->{f.get('remote_port')} {f.get('state')} queued={f.get('queued')}")
    if args.json:
        print(json.dumps({"status": status, "native_flows": flows}, indent=2))
    return 0


def cmd_tunnel(args) -> int:
    from .tunnel import main as tunnel_main

    return tunnel_main(args)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rnsfips", description="Reticulum <-> FIPS bridge")
    p.add_argument("--version", action="version", version=f"rnsfips {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("install-interface", help="install the FIPSInterface shim into a Reticulum config dir")
    s.add_argument("--configdir", default="~/.reticulum")
    s.set_defaults(func=cmd_install_interface)

    s = sub.add_parser("status", help="show the local FIPS node and native API flows")
    s.add_argument("--control-socket", default=None)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("tunnel", help="run the FIPS-over-Reticulum tunnel daemon")
    s.add_argument("--config", default="~/.config/rnsfips/tunnel.yaml", help="tunnel configuration file")
    s.add_argument("--rns-configdir", default=None, help="Reticulum config dir (default: Reticulum's own search)")
    s.add_argument("-v", "--verbose", action="count", default=0)
    s.set_defaults(func=cmd_tunnel)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
