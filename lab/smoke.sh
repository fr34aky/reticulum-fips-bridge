#!/usr/bin/env bash
# End-to-end check of Reticulum over a real FIPS mesh.
#
#   site-a <-> relay <-> site-b   (FIPS, UDP)
#   site-a <================> site-b   (Reticulum over FIPS, through the relay)
#
# Expects the image built by lab/build.sh. Leaves the lab running unless
# KEEP=0; prints logs on failure.
set -euo pipefail
cd "$(dirname "$0")"
C=(docker compose -f docker-compose.yml)
KEEP="${KEEP:-1}"

fail() { echo "SMOKE FAILED: $*" >&2; "${C[@]}" logs --tail=80 >&2 || true; exit 1; }
cleanup() { if [ "$KEEP" = "0" ]; then "${C[@]}" down -v >/dev/null 2>&1 || true; fi; }
trap cleanup EXIT

x() { "${C[@]}" exec -T "$@"; }

wait_grep() {  # wait_grep <pattern> <tries> <svc> <cmd...>
    local pat=$1 tries=$2 svc=$3; shift 3
    for _ in $(seq "$tries"); do
        if x "$svc" "$@" 2>/dev/null | grep -q -- "$pat"; then return 0; fi
        sleep 2
    done
    echo "--- last output of $svc $* ---" >&2
    x "$svc" "$@" >&2 || true
    return 1
}

echo "== starting lab"
"${C[@]}" up -d
echo "== waiting for FIPS peering"
wait_grep '"connectivity": "connected"' 45 site-a fipsctl show peers || fail "site-a has no connected FIPS peer"
wait_grep '"connectivity": "connected"' 45 site-b fipsctl show peers || fail "site-b has no connected FIPS peer"
echo "== waiting for the Reticulum FIPS peer interfaces"
wait_grep 'FIPSPeerInterface\[FIPS/' 60 site-a rnstatus || fail "site-a rnstatus shows no FIPSPeerInterface"
wait_grep 'FIPSPeerInterface\[FIPS/' 60 site-b rnstatus || fail "site-b rnstatus shows no FIPSPeerInterface"
x site-a rnstatus | sed -n '/FIPSPeerInterface/,/^$/p' | head -12

echo "== starting rncp listener on site-b"
x site-b sh -c 'rm -rf /tmp/recv; mkdir -p /tmp/recv; rm -f /tmp/rncp.log; if [ -f /tmp/rncp.pid ]; then kill "$(cat /tmp/rncp.pid)" 2>/dev/null; fi; PYTHONUNBUFFERED=1 rncp -l -n -s /tmp/recv -b 0 -vv > /tmp/rncp.log 2>&1 < /dev/null & echo $! > /tmp/rncp.pid'
wait_grep 'listening on' 15 site-b cat /tmp/rncp.log || fail "rncp did not start"
DEST=$(x site-b sh -c 'grep -o "listening on <[0-9a-f]*>" /tmp/rncp.log | head -1 | tr -d "<>" | awk "{print \$3}"')
[ -n "$DEST" ] || fail "could not read rncp destination hash"
echo "rncp destination: $DEST"

echo "== resolving path from site-a"
wait_grep 'hop.* away' 30 site-a rnpath "$DEST" || fail "no path from site-a to $DEST"
x site-a rnpath "$DEST"

echo "== transferring 300 KB with rncp"
x site-a sh -c 'head -c 300000 /dev/urandom > /tmp/payload && sha256sum /tmp/payload | cut -d" " -f1' > /tmp/rnsfips-smoke.sha
x site-a rncp /tmp/payload "$DEST" -w 180 -S || fail "rncp transfer failed"
sleep 2
GOT=$(x site-b sh -c 'sha256sum /tmp/recv/payload* | head -1 | cut -d" " -f1')
WANT=$(cat /tmp/rnsfips-smoke.sha)
[ "$GOT" = "$WANT" ] || fail "checksum mismatch: sent $WANT got $GOT"
echo "== OK: $WANT received intact on site-b"
echo "== FIPS view from site-a:"
x site-a fipsctl show native-flows | head -30
