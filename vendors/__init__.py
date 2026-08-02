"""Registry of camera/DVR vendors for the RTSP scanner.

Add a vendor: create `vendors/<name>.py` defining a `Vendor` (see `base.Vendor`),
import it here, and add it to `VENDORS` (specific vendors) — `GENERIC` stays the
last-resort fallback.
"""

import re

from .axis import AXIS
from .base import Vendor
from .dahua import DAHUA
from .foscam import FOSCAM
from .generic import GENERIC
from .hikvision import HIKVISION
from .panasonic import PANASONIC
from .reolink import REOLINK
from .uniview import UNIVIEW
from .vivotek import VIVOTEK
from .xiongmai import XIONGMAI

# Named vendors, most-common first; GENERIC is the catch-all fallback (no realm).
VENDORS = [HIKVISION, DAHUA, XIONGMAI, REOLINK, UNIVIEW, AXIS, FOSCAM, VIVOTEK, PANASONIC]
FALLBACK = [GENERIC]

# XiongMai/XM OEM realm: `Login to <long hex hash>` (vs a Dahua serial, which has
# non-hex letters). Checked before the "login to" keyword so XM wins over Dahua.
_XM_REALM = re.compile(r"login to\s+[0-9a-f]{20,}\b", re.I)


def detect(realm):
    """The Vendor implied by a device's 401 digest `realm`, or None. `Login to
    <hex hash>` -> XiongMai; a bare alphanumeric serial-number realm (no spaces,
    has a digit) -> Dahua-family."""
    r = (realm or "").strip().lower()
    if _XM_REALM.search(r):
        return XIONGMAI
    for v in VENDORS:
        if v.matches(r):
            return v
    if r and " " not in r and any(ch.isdigit() for ch in r):
        return DAHUA
    return None


def enumeration_order(realm):
    """Vendors to try when enumerating a found port: the detected one first, then
    the remaining named vendors, then the generic fallback."""
    guess = detect(realm)
    return ([guess] if guess else []) + [v for v in VENDORS if v is not guess] + FALLBACK


__all__ = ["Vendor", "VENDORS", "FALLBACK", "detect", "enumeration_order",
           "HIKVISION", "DAHUA", "XIONGMAI", "REOLINK", "UNIVIEW", "AXIS", "FOSCAM",
           "VIVOTEK", "PANASONIC", "GENERIC"]
