"""rnsfips: a bridge between the Reticulum Network Stack and the FIPS mesh.

Two pieces live here:

* ``rnsfips.interface`` is a Reticulum interface that carries Reticulum
  frames over FIPS, using the FIPS native datagram API.
* ``rnsfips.tunnel`` is a daemon that carries FIPS wire frames over
  Reticulum links and presents them to a local FIPS daemon as a UDP peer.

Reticulum is used strictly as a library. No Reticulum code is copied here.
"""

__version__ = "0.1.4"
