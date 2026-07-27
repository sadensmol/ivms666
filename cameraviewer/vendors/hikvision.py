"""Hikvision and Hikvision-OEM (std-cgi / DNVRS-Webs) DVRs/cameras.

Channel id is `<channel><stream>`: `101` = ch1 main, `102` = ch1 sub, `201` = ch2
main, … Realm on the 401 is typically `Embedded Net DVR` / `IP Camera` / `DVRNVRDVS`.
Each stream lists the canonical path first, then older-firmware syntaxes as
fallbacks (the first that works wins, so a camera yields one URL).
"""

from .base import Vendor

HIKVISION = Vendor(
    name="Hikvision",
    realm_keywords=("embedded net dvr", "ip camera", "dvrnvrdvs", "hikvision",
                    "streaming server", "webs"),
    channel_streams=(
        ("/Streaming/Channels/{c}01", "/ISAPI/Streaming/Channels/{c}01",
         "/Streaming/Unicast/channels/{c}01", "/streaming/channels/{c}01",
         "/Streaming/Channels/{c}", "/h264/ch{c}/main/av_stream",
         "/PSIA/Streaming/channels/{c}01", "/PSIA/Streaming/channels/{c}"),  # main
        ("/Streaming/Channels/{c}02", "/streaming/channels/{c}02",
         "/h264/ch{c}/sub/av_stream"),                               # sub
    ),
)
