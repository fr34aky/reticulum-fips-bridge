"""Command line entry point: ``rnsfips``."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

from . import __version__
from .rnsutil import shared_instance_reachable as _shared_instance_reachable

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


def _check_rnsd_interpreter() -> None:
    """Warn when the rnsd on PATH runs in an interpreter that cannot import rnsfips.

    Reticulum loads the shim inside rnsd's own interpreter, so rnsfips must be
    installed there. A separate pipx venv for `rns` is the usual way to get
    this wrong.
    """
    rnsd = shutil.which("rnsd")
    if not rnsd:
        print("note: no rnsd on PATH. With pipx, run: pipx inject rnsfips rns --include-apps", file=sys.stderr)
        return
    interpreter = None
    try:
        with open(rnsd, "rb") as f:
            first = f.readline().decode("utf-8", "replace").strip()
        if first.startswith("#!"):
            interpreter = first[2:].split()[0]
    except OSError:
        pass
    if not interpreter or interpreter == sys.executable:
        return
    import subprocess

    rc = subprocess.run([interpreter, "-c", "import rnsfips"], capture_output=True).returncode
    if rc != 0:
        print(f"WARNING: {rnsd} runs in {interpreter}, which cannot import rnsfips; the interface will not load there.\n"
              f"         Install rnsfips into that environment, or expose rnsd from this one "
              f"(pipx: pipx inject rnsfips rns --include-apps --force; pipx uninstall rns).", file=sys.stderr)


def cmd_install_interface(args) -> int:
    configdir = os.path.expanduser(args.configdir)
    ifdir = os.path.join(configdir, "interfaces")
    os.makedirs(ifdir, exist_ok=True)
    dest = os.path.join(ifdir, "FIPSInterface.py")
    shutil.copyfile(_shim_source(), dest)
    print(f"installed {dest}")
    config = os.path.join(configdir, "config")
    if os.path.exists(config):
        print(f"add this to the [interfaces] section of {config}:\n")
    else:
        print(f"{config} does not exist yet: run rnsd once to create it (or `rnsd --exampleconfig > {config}`),\n"
              f"set enable_transport = yes, then add this to its [interfaces] section:\n")
    print(SAMPLE_CONFIG)
    _check_rnsd_interpreter()
    return 0


RNSD_UNIT = """\
[Unit]
Description=Reticulum Network Stack daemon (rnsd)
After=network-online.target
Wants=network-online.target

[Service]
# Launched through the interpreter with -u: pipx launchers run python -E, which
# ignores PYTHONUNBUFFERED, and buffered logs reach the journal only at exit.
ExecStart={python} -u {rnsd} -v
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""

TUNNEL_UNIT = """\
[Unit]
Description=rnsfips tunnel (FIPS over Reticulum)
After=rnsd.service fips.service
Requires=rnsd.service

[Service]
Environment=PYTHONUNBUFFERED=1
# Wait for the rnsd shared instance; a tunnel started earlier would become
# its own Reticulum instance instead of a client of rnsd.
ExecStartPre=/bin/sh -c 'for i in $(seq 1 60); do {rnstatus} >/dev/null 2>&1 && exit 0; sleep 1; done; echo "rnsd shared instance not reachable" >&2; exit 1'
ExecStart={python} -u {rnsfips} tunnel --config {config} -v
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""


def _sibling(name: str) -> str:
    """Path of a console script installed next to this one, else from PATH."""
    bindir = os.path.dirname(os.path.abspath(sys.argv[0]))
    candidate = os.path.join(bindir, name)
    if os.access(candidate, os.X_OK):
        return candidate
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(f"{name} not found next to {sys.argv[0]} or on PATH")
    return found


def render_units(rnsd: str, rnstatus: str, rnsfips: str, config: str, python: str | None = None) -> dict:
    python = python or sys.executable
    return {
        "rnsd.service": RNSD_UNIT.format(rnsd=rnsd, python=python),
        "rnsfips-tunnel.service": TUNNEL_UNIT.format(rnstatus=rnstatus, rnsfips=rnsfips, config=config, python=python),
    }


def cmd_install_units(args) -> int:
    if args.system:
        unitdir = "/etc/systemd/system"
        config = args.tunnel_config or "/etc/rnsfips/tunnel.conf"
        ctl = "systemctl"
    else:
        unitdir = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "systemd", "user")
        config = args.tunnel_config or "%h/.config/rnsfips/tunnel.conf"
        ctl = "systemctl --user"
    if args.unit_dir:
        unitdir = args.unit_dir
    try:
        units = render_units(_sibling("rnsd"), _sibling("rnstatus"), _sibling("rnsfips"), config)
    except FileNotFoundError as e:
        print(f"error: {e}. With pipx: pipx inject rnsfips rns --include-apps", file=sys.stderr)
        return 1
    os.makedirs(unitdir, exist_ok=True)
    for name, body in units.items():
        path = os.path.join(unitdir, name)
        with open(path, "w") as f:
            f.write(body)
        print(f"wrote {path}")
    print(f"""
next:
  {ctl} daemon-reload
  {ctl} enable --now rnsd.service              # Reticulum with the FIPS interface
  {ctl} enable --now rnsfips-tunnel.service    # only where a tunnel.conf exists
  {ctl} status rnsd rnsfips-tunnel
""")
    if not args.system:
        print("user units stop at logout unless lingering is enabled: loginctl enable-linger $USER")
    return 0


def _reticulum_view() -> dict:
    if not _shared_instance_reachable():
        return {"error": "rnsd shared instance not reachable"}
    import RNS

    try:
        reticulum = RNS.Reticulum(require_shared_instance=True, loglevel=RNS.LOG_ERROR)
        stats = reticulum.get_interface_stats()
    except SystemExit:
        return {"error": "could not connect to the rnsd shared instance"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    ifaces = []
    for i in stats.get("interfaces", []):
        if i.get("type") not in ("FIPSInterface", "FIPSPeerInterface"):
            continue
        ifaces.append({k: i.get(k) for k in ("name", "type", "status", "mtu", "mode", "rxb", "txb", "clients", "peers")})
    from .discovery import load_state

    discovered = load_state(RNS.Reticulum.storagepath).get("bridges", {})
    return {"interfaces": ifaces, "transport_id": stats.get("transport_id").hex() if stats.get("transport_id") else None,
            "discovered_bridges": discovered}


def _fips_view(control_socket: str | None) -> dict:
    from .fipsctl import FipsControl, FipsControlError

    ctl = FipsControl(control_socket)
    try:
        status = ctl.show_status()
        flows = ctl.show_native_flows()
        peers = ctl.show_peers()
    except FipsControlError as e:
        return {"error": str(e)}
    return {
        "npub": status.get("npub"), "state": status.get("state"), "version": status.get("version"),
        "peer_count": status.get("peer_count"), "ipv6_mtu": status.get("effective_ipv6_mtu"),
        "listeners": [l.get("local_port") for l in flows.get("listeners", [])],
        "flows": [{k: f.get(k) for k in ("peer", "local_port", "remote_port", "state", "queued")} for f in flows.get("flows", [])],
        "native_stats": {k: v for k, v in (flows.get("stats") or {}).items() if v},
        "loopback_peers": [{"npub": p.get("npub"), "addr": p.get("transport_addr"), "connectivity": p.get("connectivity")}
                           for p in peers if str(p.get("transport_addr", "")).startswith("127.")],
    }


def _tunnel_view(socket_path: str | None) -> dict:
    from .tunnel import control_query, default_control_socket

    path = socket_path or default_control_socket()
    if not os.path.exists(path):
        return {"error": f"no tunnel control socket at {path} (tunnel not running?)"}
    try:
        return control_query("status", socket_path=path)
    except ConnectionRefusedError:
        return {"error": f"stale control socket at {path}: the tunnel that created it is gone "
                         "(killed without cleanup, or a second tunnel took the path); restart the tunnel"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _mode_name(mode):
    names = {1: "full", 2: "point-to-point", 3: "access-point", 4: "roaming", 5: "boundary", 6: "gateway", 7: "internal"}
    return names.get(mode, str(mode))


def _fmt_bytes(n) -> str:
    n = n or 0
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def cmd_status(args) -> int:
    view = {"fips": _fips_view(args.control_socket), "reticulum": _reticulum_view(), "tunnel": _tunnel_view(args.tunnel_socket)}
    if args.json:
        print(json.dumps(view, indent=2, default=str))
        return 0

    f = view["fips"]
    print("FIPS node")
    if "error" in f:
        print(f"  unavailable: {f['error']}")
    else:
        print(f"  {f['npub']}  {f['state']}  {f['version']}  peers={f['peer_count']}  ipv6_mtu={f['ipv6_mtu']}")
        print(f"  native API listeners: {f['listeners'] or 'none'}")
        for fl in f["flows"]:
            print(f"  flow {fl['peer'][:20]}…  {fl['local_port']}->{fl['remote_port']}  {fl['state']}  queued={fl['queued']}")
        for p in f["loopback_peers"]:
            print(f"  tunnel peer {p['npub'][:20]}…  via {p['addr']}  {p['connectivity']}")
        drops = {k: v for k, v in f["native_stats"].items() if k.startswith("drop_")}
        if drops:
            print(f"  native drops: {drops}")

    r = view["reticulum"]
    print("Reticulum (rnsd)")
    if "error" in r:
        print(f"  unavailable: {r['error']}")
    else:
        if not r["interfaces"]:
            print("  no FIPS interfaces configured")
        for i in r["interfaces"]:
            up = "up" if i["status"] else "down"
            extra = f"  clients={i['clients']}" if i["type"] == "FIPSInterface" else ""
            print(f"  {i['name']}  {up}  mtu={i['mtu']}  mode={_mode_name(i['mode'])}  rx={_fmt_bytes(i['rxb'])} tx={_fmt_bytes(i['txb'])}{extra}")
        import time as _time

        for npub, d in sorted(r.get("discovered_bridges", {}).items(), key=lambda kv: -float(kv[1].get("seen", 0))):
            age = _time.time() - float(d.get("seen", 0))
            print(f"  discovered bridge {npub}  port={d.get('port')}  seen {age/60:.0f} min ago")

    t = view["tunnel"]
    print("Tunnel (rnsfips tunnel)")
    if "error" in t:
        print(f"  {t['error']}")
    else:
        print(f"  destination {t.get('destination')}  for FIPS node {str(t.get('npub'))[:20]}…")
        for p in t.get("peers", []):
            state = "up" if p["up"] else "down"
            who = p["dest"] or "inbound"
            print(f"  peer {who}  {state}  {'dialled' if p['initiator'] else 'accepted'}  npub={str(p['npub'])[:20]}…  "
                  f"udp={p['udp_port']}  mdu={p['mdu']}  tx={p['tx_frames']} rx={p['rx_frames']} drops={p['tx_drops']}")
        for h, d in (t.get("discovered") or {}).items():
            print(f"  discovered {h}  npub={str(d.get('npub'))[:20]}…  {d.get('impl')} {d.get('ver')}")
    return 0


def cmd_tunnel_ctl(args) -> int:
    from .tunnel import control_query

    params = {}
    if args.dest:
        params["dest"] = args.dest
    try:
        data = control_query(args.command, params, socket_path=args.tunnel_socket)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(data, indent=2, default=str))
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

    s = sub.add_parser("install-units", help="write systemd units for rnsd and the tunnel")
    s.add_argument("--system", action="store_true", help="write system units to /etc/systemd/system (run as root)")
    s.add_argument("--unit-dir", default=None, help="override the unit directory")
    s.add_argument("--tunnel-config", default=None, help="tunnel config path used by the tunnel unit")
    s.set_defaults(func=cmd_install_units)

    s = sub.add_parser("status", help="show the FIPS node, the Reticulum FIPS interfaces and the tunnel")
    s.add_argument("--control-socket", default=None, help="FIPS control socket path")
    s.add_argument("--tunnel-socket", default=None, help="tunnel control socket path")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("tunnel-ctl", help="send a command to the running tunnel")
    s.add_argument("command", choices=["status", "announce", "add_peer", "remove_peer"])
    s.add_argument("dest", nargs="?", help="destination hash for add_peer/remove_peer")
    s.add_argument("--tunnel-socket", default=None)
    s.set_defaults(func=cmd_tunnel_ctl)

    s = sub.add_parser("tunnel", help="run the FIPS-over-Reticulum tunnel daemon")
    s.add_argument("--config", default="~/.config/rnsfips/tunnel.conf", help="tunnel configuration file")
    s.add_argument("--rns-configdir", default=None, help="Reticulum config dir (default: Reticulum's own search)")
    s.add_argument("-v", "--verbose", action="count", default=0)
    s.set_defaults(func=cmd_tunnel)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
