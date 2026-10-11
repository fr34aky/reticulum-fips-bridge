# Reticulum custom interface shim for rnsfips.
#
# `rnsfips install-interface` copies this file to
# ~/.reticulum/interfaces/FIPSInterface.py. Then and add a block like this to the Reticulum
# configuration:
#
#   [[FIPS]]
#     type = FIPSInterface
#     enabled = yes
#     mode = full
#     listen_port = 21075
#     peers = npub1..., npub1...
#
# Reticulum exec()s this file with only `Interface` and `RNS` in scope, so the
# real implementation lives in the rnsfips package. The package is found on
# rnsd's own sys.path; RNSFIPS_PATH or a copy placed next to this file under
# ~/.reticulum/interfaces/ are supported as fallbacks.

import os
import sys

_extra = os.environ.get("RNSFIPS_PATH")
if _extra and _extra not in sys.path:
    sys.path.insert(0, _extra)
_ifpath = getattr(RNS.Reticulum, "interfacepath", None)
if _ifpath and _ifpath not in sys.path:
    sys.path.append(_ifpath)

import rnsfips
from rnsfips.interface import FIPSInterface as interface_class

RNS.log(f"rnsfips {rnsfips.__version__} loaded from {os.path.dirname(rnsfips.__file__)}", RNS.LOG_DEBUG)
