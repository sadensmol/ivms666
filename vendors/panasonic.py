"""Panasonic cameras (`nphMpeg4/*`, `MediaInput/*`)."""

from .base import Vendor

PANASONIC = Vendor(
    name="Panasonic",
    realm_keywords=("panasonic",),
    single_streams=(
        ("/MediaInput/h264", "/nphMpeg4/g726-640x480", "/nph-h264.cgi"),   # main
        ("/MediaInput/mpeg4", "/nphMpeg4/nil-320x240"),                    # sub / mpeg4
        ("/ONVIF/MediaInput",),
    ),
)
