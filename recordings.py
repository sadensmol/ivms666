"""Motion event log + clip playback from the DVR's recordings (ISAPI).

With the DVR set to **motion-triggered** recording (see diagnose), each recorded
segment IS a motion event, padded by the pre/post-record. We list them with
CMSearch (`POST /ISAPI/ContentMgmt/search`) and play a clip by transcoding its
RTSP playback range to a browser-friendly MP4 with ffmpeg.

Times are the DVR's own wall clock, labeled with a trailing `Z` (NOT real UTC —
no timezone conversion). CMSearch returns ISO `2026-07-26T08:01:34Z`; the RTSP
playback URL wants `20260726T080134Z`.
"""

import re
import time
from datetime import datetime

import camera, live, playback

_offset_cache = {}  # host -> (offset_secs, expiry_monotonic)


def time_offset(cfg):
    """Seconds to ADD to a DVR wall-clock time to get THIS host's wall clock — the
    DVR's clock is often set wrong (measured ~4.8h off), so the app's saved
    snapshots (host time) and the DVR's event times don't line up without it.
    Cached ~5 min; returns 0.0 on any failure."""
    host = cfg.get("host", "")
    cached = _offset_cache.get(host)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    off = 0.0
    try:
        _, raw = camera.camera_get(cfg, "/ISAPI/System/time", timeout=8)
        m = re.search(r"<localTime>(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", raw.decode("utf-8", "replace"))
        if m:
            off = (datetime.now() - datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")).total_seconds()
    except Exception:  # noqa: BLE001 - offset just falls back to 0
        pass
    _offset_cache[host] = (off, time.monotonic() + 300)
    return off

SEARCH = "/ISAPI/ContentMgmt/search"
# Audio is dropped (-an): this DVR records G.711/pcm_mulaw, which can't be
# stream-copied into MP4. `frag_keyframe+empty_moov` makes a fragmented MP4 that
# an HTML5 <video> can start playing as bytes arrive (no full-file wait).
# `-timeout` (µs) matters here as much as the muxer flags: without it a clip whose
# RTSP stream stalls hangs forever, and that hung ffmpeg holds the DVR's single
# session — every event thumbnail then comes back 453.
_FFMPEG_CLIP = ["ffmpeg", "-nostdin", "-loglevel", "error", "-rtsp_transport", "tcp",
                "-timeout", live.RTSP_TIMEOUT_US,
                "-i", "{url}", "-an", "-c:v", "copy",
                "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "-"]


def _iso_to_rtsp(iso):
    """'2026-07-26T08:01:34Z' (or without Z) -> '20260726T080134Z'."""
    digits = re.sub(r"[^0-9T]", "", iso.replace("Z", ""))  # '20260726T080134'
    return digits + "Z"


def _cmsearch_body(track_id, start_iso, end_iso, max_results):
    # searchID must be a well-formed GUID or the DVR rejects it (400).
    return (
        '<CMSearchDescription><searchID>0FEEDEE0-0000-0000-0000-000000000001</searchID>'
        f"<trackList><trackID>{track_id}</trackID></trackList>"
        f"<timeSpanList><timeSpan><startTime>{start_iso}</startTime>"
        f"<endTime>{end_iso}</endTime></timeSpan></timeSpanList>"
        f"<maxResults>{int(max_results)}</maxResults>"
        "<searchResultPostion>0</searchResultPostion></CMSearchDescription>"
    ).encode()


def _seconds(start_iso, end_iso):
    def secs(s):
        h, m, sec = s[11:19].split(":")
        return int(h) * 3600 + int(m) * 60 + int(sec)
    try:
        return max(0, secs(end_iso) - secs(start_iso))
    except Exception:  # noqa: BLE001
        return 0


def list_events(cfg, track_id, start_iso, end_iso, max_results=100):
    """Return motion events for a track in [start_iso, end_iso], newest first:
      [{time, seconds, start, end}] — `time` is the ISO start (DVR clock),
      `start`/`end` are the RTSP-format bounds for /clip."""
    _, raw = camera.camera_post(cfg, SEARCH, _cmsearch_body(track_id, start_iso, end_iso, max_results))
    text = raw.decode("utf-8", "replace")
    events = []
    for m in re.finditer(r"<startTime>([^<]+)</startTime>\s*<endTime>([^<]+)</endTime>", text):
        s, e = m.group(1), m.group(2)
        events.append({"time": s.replace("Z", ""), "seconds": _seconds(s, e),
                       "start": _iso_to_rtsp(s), "end": _iso_to_rtsp(e)})
    events.sort(key=lambda ev: ev["start"], reverse=True)
    return events


def clip_process(cfg, track_id, start, end):
    """Spawn ffmpeg to stream one recorded clip [start, end] (RTSP-format times)
    as fragmented MP4 on stdout. Caller terminates it when the client leaves."""
    url = playback.playback_url(cfg, track_id, start, end)
    cmd = [a.format(url=url) for a in _FFMPEG_CLIP]
    return live.spawn(cmd)


ffmpeg_available = live.ffmpeg_available
terminate = live.terminate
