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
ExecStart={rnsd} -v
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
ExecStart={rnsfips} tunnel --config {config} -v
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


def render_units(rnsd: str, rnstatus: str, rnsfips: str, config: str) -> dict:
    return {
        "rnsd.service": RNSD_UNIT.format(rnsd=rnsd),
        "rnsfips-tunnel.service": TUNNEL_UNIT.format(rnstatus=rnstatus, rnsfips=rnsfips, config=config),
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

    s = sub.add_parser("install-units", help="write systemd units for rnsd and the tunnel")
    s.add_argument("--system", action="store_true", help="write system units to /etc/systemd/system (run as root)")
    s.add_argument("--unit-dir", default=None, help="override the unit directory")
    s.add_argument("--tunnel-config", default=None, help="tunnel config path used by the tunnel unit")
    s.set_defaults(func=cmd_install_units)

    s = sub.add_parser("status", help="show the local FIPS node and native API flows")
    s.add_argument("--control-socket", default=None)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

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
