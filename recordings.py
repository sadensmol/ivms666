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
from datetime import datetime, timedelta, timezone

import camera, live, playback

_clock_cache = {}  # host -> ((skew_secs, tzinfo), expiry_monotonic)


def _clock(cfg):
    """(skew_secs, tzinfo) for a device.

    A DVR timestamp is its own wall clock, and that clock drifts badly (~6h seen),
    so it is NOT a real instant: real_epoch = dvr_wall_clock (read in `tzinfo`,
    the offset the DVR itself reports) + `skew`. With that, event times can be sent
    to the browser as real epochs and rendered in the VIEWER's timezone, instead of
    showing the DVR's wrong local clock. Cached ~5 min; falls back to (0, local tz)
    so a device that won't answer just behaves as before.
    """
    host = cfg.get("host", "")
    cached = _clock_cache.get(host)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    info = (0.0, datetime.now().astimezone().tzinfo)
    try:
        _, raw = camera.camera_get(cfg, "/ISAPI/System/time", timeout=8)
        m = re.search(r"<localTime>([^<]+)</localTime>", raw.decode("utf-8", "replace"))
        if m:
            dvr = datetime.fromisoformat(m.group(1).strip())
            if dvr.tzinfo is None:   # no offset advertised -> assume this host's
                dvr = dvr.replace(tzinfo=info[1])
            info = (time.time() - dvr.timestamp(), dvr.tzinfo)
    except Exception:  # noqa: BLE001 - an unreadable clock just means "no correction"
        pass
    _clock_cache[host] = (info, time.monotonic() + 300)
    return info


def dvr_window(cfg, hours):
    """(start_iso, end_iso) covering the last `hours` of REAL time, expressed in the
    DVR's own drifting wall clock — the only clock CMSearch understands. Using the
    host's clock here would search a window the DVR reads as hours off, silently
    trimming the recording at one end."""
    skew, tz = _clock(cfg)
    now = datetime.fromtimestamp(time.time() - skew, tz)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (now - timedelta(hours=hours)).strftime(fmt), now.strftime(fmt)


def _epoch(iso, skew, tz):
    """DVR wall-clock ISO -> real UTC epoch seconds (None if unparseable)."""
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "")).replace(tzinfo=tz).timestamp() + skew)
    except ValueError:
        return None

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


def _cmsearch_body(track_id, start_iso, end_iso, max_results, position=0):
    # searchID must be a well-formed GUID or the DVR rejects it (400).
    return (
        '<CMSearchDescription><searchID>0FEEDEE0-0000-0000-0000-000000000001</searchID>'
        f"<trackList><trackID>{track_id}</trackID></trackList>"
        f"<timeSpanList><timeSpan><startTime>{start_iso}</startTime>"
        f"<endTime>{end_iso}</endTime></timeSpan></timeSpanList>"
        f"<maxResults>{int(max_results)}</maxResults>"
        f"<searchResultPostion>{int(position)}</searchResultPostion></CMSearchDescription>"
    ).encode()


def _seconds(start_iso, end_iso):
    def secs(s):
        h, m, sec = s[11:19].split(":")
        return int(h) * 3600 + int(m) * 60 + int(sec)
    try:
        return max(0, secs(end_iso) - secs(start_iso))
    except Exception:  # noqa: BLE001
        return 0


PAGE_SIZE = 100  # per request; this DVR truncates to ~64 anyway and answers "MORE"


def _page(cfg, track_id, start_iso, end_iso, limit, position):
    """One CMSearch page -> (events, more?)."""
    _, raw = camera.camera_post(cfg, SEARCH,
                                _cmsearch_body(track_id, start_iso, end_iso, limit, position))
    text = raw.decode("utf-8", "replace")
    events = []
    for m in re.finditer(r"<startTime>([^<]+)</startTime>\s*<endTime>([^<]+)</endTime>", text):
        s, e = m.group(1), m.group(2)
        events.append({"time": s.replace("Z", ""), "seconds": _seconds(s, e),
                       "start": _iso_to_rtsp(s), "end": _iso_to_rtsp(e)})
    status = re.search(r"<responseStatusStrg>([^<]*)</responseStatusStrg>", text)
    return events, bool(status) and status.group(1).strip().upper() == "MORE"


def list_events(cfg, track_id, start_iso, end_iso, max_results=500):
    """Return motion events for a track in [start_iso, end_iso], newest first:
      [{time, epoch, seconds, start, end}] — `time` is the ISO start (DVR clock,
      what /playback and /clip want), `epoch` the same instant as real UTC seconds
      so the browser can show it in the VIEWER's timezone, and `start`/`end` are the
      RTSP-format bounds for /clip.

    Paged: the DVR truncates one search to ~64 matches, flags `responseStatusStrg`
    MORE, and returns the OLDEST matches first — so a single request silently drops
    the NEWEST events (the log looked like it stopped hours ago). Keep asking with a
    higher `searchResultPostion` until it stops saying MORE; `max_results` bounds a
    device that always does."""
    events, pos = [], 0
    while len(events) < max_results:
        page, more = _page(cfg, track_id, start_iso, end_iso,
                           min(PAGE_SIZE, max_results - len(events)), pos)
        events.extend(page)
        if not page or not more:
            break
        pos += len(page)
    skew, tz = _clock(cfg)
    for ev in events:
        ev["epoch"] = _epoch(ev["time"], skew, tz)
    events.sort(key=lambda ev: ev["start"], reverse=True)
    return events[:max_results]


def clip_process(cfg, track_id, start, end):
    """Spawn ffmpeg to stream one recorded clip [start, end] (RTSP-format times)
    as fragmented MP4 on stdout. Caller terminates it when the client leaves."""
    url = playback.playback_url(cfg, track_id, start, end)
    cmd = [a.format(url=url) for a in _FFMPEG_CLIP]
    return live.spawn(cmd)


ffmpeg_available = live.ffmpeg_available
terminate = live.terminate
