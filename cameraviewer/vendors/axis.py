"""Axis cameras. Single stream by default; multi-imager models take `?camera=<n>`."""

from .base import Vendor

AXIS = Vendor(
    name="Axis",
    realm_keywords=("axis",),
    channel_streams=(
        ("/axis-media/media.amp?camera={c}",),
    ),
    single_streams=(
        ("/axis-media/media.amp", "/axis-media/media.amp?videocodec=h264",
         "/mpeg4/media.amp", "/media.amp", "/media.amp?streamprofile=Profile1"),
    ),
)
