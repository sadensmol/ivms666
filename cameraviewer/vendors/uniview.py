"""Uniview (UNV) cameras/NVRs. Some models also accept the Dahua-style path."""

from .base import Vendor

UNIVIEW = Vendor(
    name="Uniview",
    realm_keywords=("uniview", "unv"),
    channel_streams=(
        ("/unicast/c{c}/s0/live", "/media/video{c}", "/video{c}", "/ch{c}/0"),  # main
        ("/unicast/c{c}/s1/live",),                                             # sub
    ),
)
