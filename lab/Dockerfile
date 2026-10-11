# Lab node: FIPS daemon + Reticulum (rnsd) + rnsfips.
#
# Build context is the repository root:  docker build -f lab/Dockerfile .
# The FIPS binaries come from lab/bin/ (see lab/build.sh), so the image does
# not need a Rust toolchain. Debian trixie ships glibc 2.41, libdbus and
# libsystemd, which is all the binaries need.

FROM debian:trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip ca-certificates \
        libdbus-1-3 libsystemd0 iproute2 iputils-ping procps \
    && rm -rf /var/lib/apt/lists/*

COPY lab/bin/fips lab/bin/fipsctl /usr/local/bin/
RUN chmod +x /usr/local/bin/fips /usr/local/bin/fipsctl && mkdir -p /run/fips /etc/fips /shared

RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir rns
ENV PATH="/opt/venv/bin:${PATH}"

COPY pyproject.toml README.md LICENSE /opt/rnsfips/
COPY src /opt/rnsfips/src
RUN /opt/venv/bin/pip install --no-cache-dir /opt/rnsfips

COPY lab/entry.py /usr/local/bin/entry.py
ENTRYPOINT ["/opt/venv/bin/python3", "/usr/local/bin/entry.py"]
