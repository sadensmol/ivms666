"""Foscam cameras. Single-stream endpoints (main / sub)."""

from .base import Vendor

FOSCAM = Vendor(
    name="Foscam",
    realm_keywords=("foscam",),
    single_streams=(
        ("/videoMain", "/video.h264"),   # main
        ("/videoSub",),                  # sub
    ),
)
