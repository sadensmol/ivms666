"""Catch-all schemas for unknown/white-label ONVIF-ish devices. Tried only as a
last resort, after every named vendor, when the realm gives no hint.

The channel group lists many alternate syntaxes for "this channel's stream" (first
that works wins, per channel); the single groups cover common fixed endpoints. This
is a broad, curated dictionary of real RTSP paths seen across cheap OEM gear —
snapshot/HTTP endpoints (`.jpg`, `.cgi`, `.mjpg`, `.asf`, `.mp4`), resolution
labels (`720p`, `HD`, `4K`), placeholders (`test`, `access_code`, `user_defined`)
and creds-in-path forms (`/user=admin&password=...`) are deliberately excluded
(not RTSP streams / not probeable without embedding credentials).

`{c}` = 1-based channel, `{c0}` = 0-based (channel-1).
"""

from .base import Vendor

GENERIC = Vendor(
    name="Generic",
    realm_keywords=(),      # never matched by realm; used only as the fallback
    channel_streams=(
        ("/live/{c}", "/live/ch{c}/main", "/stream{c}", "/stream/{c}",
         "/streaming/channels/{c}", "/ch{c}_0.h264", "/ch{c}_0", "/ch{c}/0",
         "/cam{c}/h264", "/cam{c}", "/video{c}", "/onvif{c}", "/ONVIF/channel{c}",
         "/profile{c}", "/profile{c}/media.smp", "/play{c}.sdp", "/live{c}.sdp",
         "/medias{c}", "/PSIA/Streaming/channels/{c}",
         "/rtsp_live{c0}", "/live{c0}.264"),
    ),
    single_streams=(
        ("/live", "/live/main", "/live/main0", "/live/av0", "/live/h264",
         "/live/mpeg4", "/live/ch0", "/live/ch00_0", "/livestream", "/live_st1",
         "/live_h264.sdp", "/live_mpeg4.sdp"),
        ("/live/sub", "/live/ch00_1"),
        ("/stream", "/stream/0", "/stream.sdp", "/stream/live.sdp"),
        ("/h264", "/h264.sdp", "/h264_stream", "/h264_vga.sdp", "/rtsph264"),
        ("/main",), ("/media",), ("/mp4",),
        ("/mpeg4", "/mpeg4unicast", "/mpeg4cif", "/mpeg4/media.smp"),
        ("/video", "/video0", "/video.h264", "/videoMain", "/videostream.asf"),
        ("/media.amp", "/h264/media.amp", "/mpeg/media.amp", "/media/media.amp",
         "/onvif-media/media.amp", "/mpg4/rtsp.amp"),
        ("/av0_0", "/av0_1", "/tcp/av0_0", "/udp/av0_0", "/AVStream1_1"),
        ("/cam0", "/cam0_0", "/cam0_1"),
        ("/ch0_unicast_firststream",), ("/ch0_unicast_secondstream",),
        ("/11",), ("/12",), ("/0",), ("/1",),
        ("/ch0", "/ch0.h264", "/ch0_0.h264"), ("/ch00/0",), ("/ch001.sdp",),
        ("/ch01.264",),
        ("/ipcam.sdp", "/ipcam_h264.sdp"),
        ("/HighResolutionVideo",), ("/LowResolutionVideo",),
        ("/rtsp_tunnel",),                    # Bosch
        ("/rtpvideo1.sdp",), ("/trackID=1",), ("/gnz_media/main",),  # Ganz
        ("/multicaststream",), ("/udpstream",), ("/user.sdp",),
        ("/ucast/11",), ("/StdCh1",), ("/CH001.sdp",), ("/channel1",),
    ),
)
