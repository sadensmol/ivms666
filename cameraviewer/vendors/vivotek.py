"""Vivotek cameras. `/liveN.sdp` per stream profile, plus ONVIF Profile-S `.stm`."""

from .base import Vendor

VIVOTEK = Vendor(
    name="Vivotek",
    realm_keywords=("vivotek",),
    channel_streams=(
        ("/live{c}.sdp", "/videoinput_{c}/h264_1/media.stm",
         "/videoinput_{c}:0/h264_1/onvif.stm", "/VideoInput/{c}/h264/1"),   # main
        ("/VideoInput/{c}/mpeg4/1",),                                       # sub
    ),
    single_streams=(
        ("/live.sdp", "/liveMain"),
    ),
)
