"""Reolink cameras/NVRs. Channel is zero-padded 2-digit (`01`, `02`, …)."""

from .base import Vendor

REOLINK = Vendor(
    name="Reolink",
    realm_keywords=("reolink",),
    channel_streams=(
        ("/h264Preview_{c:02d}_main", "/Preview_{c:02d}_main"),   # main
        ("/h264Preview_{c:02d}_sub",),                            # sub
    ),
)
