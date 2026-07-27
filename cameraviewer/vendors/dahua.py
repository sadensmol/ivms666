"""Dahua and Dahua-OEM (Amcrest, Lorex, and many white-label DVRs).

The 401 digest realm is usually the device **serial number** (sometimes shown as
`Login to <serial>`), which `scan` also treats as Dahua-family when the realm is a
bare alphanumeric serial. Channel is 1-based; `subtype=0` main, `subtype=1` sub.
"""

from .base import Vendor

DAHUA = Vendor(
    name="Dahua",
    realm_keywords=("login to", "dahua", "amcrest", "lorex", "product name"),
    channel_streams=(
        ("/cam/realmonitor?channel={c}&subtype=0",),   # main
        ("/cam/realmonitor?channel={c}&subtype=1",),   # sub
    ),
    single_streams=(
        ("/cam/realmonitor",),                         # no-param default (some OEM)
    ),
)
