#!/usr/bin/env bash
# Collect the FIPS binaries for the lab image and build it.
#
# By default the host's own fips/fipsctl are used (they must be glibc builds
# needing glibc <= 2.41, which an Arch or Debian build is). Set FIPS_BIN_DIR
# to point at another pair of binaries.
set -euo pipefail
cd "$(dirname "$0")/.."

BIN_DIR="${FIPS_BIN_DIR:-}"
mkdir -p lab/bin
if [ -n "$BIN_DIR" ]; then
    cp "$BIN_DIR/fips" "$BIN_DIR/fipsctl" lab/bin/
else
    cp "$(command -v fips)" "$(command -v fipsctl)" lab/bin/
fi
echo "using $(lab/bin/fips --version 2>/dev/null | head -1)"
docker compose -f lab/docker-compose.yml build "$@"
