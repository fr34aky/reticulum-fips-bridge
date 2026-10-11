#!/usr/bin/env python3
"""Lab node entrypoint: start FIPS, exchange npubs over /shared, peer, start rnsd.

Environment:
  NODE_NAME    this node's name (also its hostname on the compose networks)
  FIPS_PEERS   comma-separated node names to peer with at the FIPS layer
  RNS_ENABLED  "1" to run rnsd with the FIPS interface
  RNS_PEERS    comma-separated node names whose FIPS npubs the interface dials
  RNS_PORT     FIPS application port for the bridge (default 21075)
"""

import os
import signal
import subprocess
import sys
import time

SHARED = "/shared"
FIPS_CFG = "/etc/fips/fips.yaml"
RNS_DIR = "/root/.reticulum"


def log(msg):
    print(f"[entry {os.environ.get('NODE_NAME', '?')}] {msg}", flush=True)


def env_list(name):
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


def write_fips_config(rns_enabled, peers=None):
    cfg = f"""\
node:
  identity:
    persistent: true
  native_api:
    enabled: {"true" if rns_enabled else "false"}
tun:
  enabled: false
dns:
  enabled: true
  bind_addr: "::1"
  port: 5354
transports:
  udp:
    bind_addr: "0.0.0.0:2121"
    mtu: 1280
"""
    if peers:
        cfg += "peers:\n"
        for name, npub in peers.items():
            cfg += f"""  - npub: "{npub}"
    alias: "{name}"
    addresses:
      - transport: udp
        addr: "{name}:2121"
    connect_policy: auto_connect
"""
    with open(FIPS_CFG, "w") as f:
        f.write(cfg)


def wait_file(path, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            with open(path) as f:
                content = f.read().strip()
            if content:
                return content
        time.sleep(0.5)
    raise SystemExit(f"timed out waiting for {path}")


def write_rns_config(peers_npubs, port):
    os.makedirs(os.path.join(RNS_DIR, "interfaces"), exist_ok=True)
    subprocess.check_call(["rnsfips", "install-interface", "--configdir", RNS_DIR], stdout=subprocess.DEVNULL)
    peers = ", ".join(f"{n}:{port}" for n in peers_npubs)
    cfg = f"""\
[reticulum]
  enable_transport = Yes
  share_instance = Yes
  panic_on_interface_error = No

[logging]
  loglevel = 5

[interfaces]
  [[FIPS]]
    type = FIPSInterface
    enabled = yes
    mode = full
    listen_port = {port}
    peers = {peers}
"""
    with open(os.path.join(RNS_DIR, "config"), "w") as f:
        f.write(cfg)


def main():
    name = os.environ["NODE_NAME"]
    fips_peers = env_list("FIPS_PEERS")
    rns_enabled = os.environ.get("RNS_ENABLED", "0") == "1"
    rns_peers = env_list("RNS_PEERS")
    port = int(os.environ.get("RNS_PORT", "21075"))

    # Stage 1: start FIPS once to mint a persistent identity, publish the npub.
    write_fips_config(rns_enabled)
    first = subprocess.Popen(["fips", "-c", FIPS_CFG])
    npub = wait_file("/etc/fips/fips.pub")
    first.terminate()
    first.wait(timeout=15)
    with open(os.path.join(SHARED, f"{name}.npub"), "w") as f:
        f.write(npub + "\n")
    log(f"npub {npub}")

    # Stage 2: wait for the peers' npubs, then start FIPS with static peers.
    peer_npubs = {p: wait_file(os.path.join(SHARED, f"{p}.npub")) for p in fips_peers}
    rns_npubs = [wait_file(os.path.join(SHARED, f"{p}.npub")) for p in rns_peers]
    write_fips_config(rns_enabled, peer_npubs)
    procs = []
    procs.append(subprocess.Popen(["fips", "-c", FIPS_CFG]))

    from rnsfips.fipsctl import FipsControl, FipsControlError

    ctl = FipsControl()
    deadline = time.time() + 60
    while True:
        try:
            ctl.show_status()
            break
        except FipsControlError:
            if time.time() > deadline:
                raise SystemExit("fips control socket never came up")
            time.sleep(0.5)

    if rns_enabled:
        write_rns_config(rns_npubs, port)
        procs.append(subprocess.Popen(["rnsd", "-v"]))

    def shutdown(*_):
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while True:
        for p in procs:
            if p.poll() is not None:
                log(f"process {p.args[0]} exited with {p.returncode}")
                shutdown()
        time.sleep(1)


if __name__ == "__main__":
    main()
