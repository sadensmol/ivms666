"""Health check for a DVR's motion -> notification pipeline, plus a safe auto-fix
for the linkage gaps it can repair.

Per *live* video-input channel (has a camera and is shown in the app) it checks:
  - motion detection enabled + an area actually painted (cellsOn > 0)
  - the VMD event trigger links `email`  (so the DVR can e-mail on motion)
  - the VMD event trigger links `center` (so the event reaches the ISAPI alert
    stream that drives this app's live motion indicator — see the CLAUDE.md
    gotcha; without `center`, `record`/`email` still fire but the app never sees
    the event)
  - motion-triggered recording at 10s pre/post and at the camera's max resolution

For a channel that shouldn't be recorded at all — either the input has **no
camera** (`videoInputEnabled=false` / `resDesc=NO VIDEO`, an empty DVR slot) or
the user **hid** its tile in the app — the normal checks are skipped (they'd 403
on an empty input and just nag about a camera you don't use). Instead we flag the
one thing that wastes disk: recording still left ON. That's auto-fixable (disable
the record track).

Device-wide it checks:
  - SMTP/mailing is configured (a server + at least one recipient)
  - the DVR clock matches real local time (bad clock -> wrong recording/OSD/email
    timestamps); auto-fixable by writing the correct local time.

Auto-fixable: missing `email`/`center` linkage, wrong recording mode / pre-post /
resolution, recording left on for an empty-or-hidden channel, and a wrong clock —
all via read-modify-write that preserves everything else. Not auto-fixable
(reported with guidance): motion disabled, no area painted, SMTP unconfigured.
"""

import re
from datetime import datetime, timedelta, timezone

import camera, motion

CLOCK_TOLERANCE = 120  # seconds; flag the DVR clock if it's off by more than this
TIME_EP = "/ISAPI/System/time"

VMD_TRIGGER = "/ISAPI/Event/triggers/VMD-{n}"
MOTION_EP = "/ISAPI/System/Video/inputs/channels/{n}/motionDetection"
VIDEO_INPUTS = "/ISAPI/System/Video/inputs/channels"
MAILING_EPS = ("/ISAPI/System/Network/mailing", "/ISAPI/System/Network/mailing/1")
RECORD_TRACK = "/ISAPI/ContentMgmt/record/tracks/{tid}"
STREAM_MAIN = "/ISAPI/Streaming/channels/{cid}"

# Motion-recording target: record on MOTION with a 10s pre/post-record pad so each
# clip starts 10s before and ends 10s after the detected motion.
REC_MODE = "MOTION"
PREPOST_SECS = 10
# Recording captures the main stream, so recording quality == main-stream
# resolution. Target the camera's max (from capabilities); fall back to Full HD.
FULLHD = (1920, 1080)
MIN_HD_HEIGHT = 720  # anything below this isn't even HD

# diagnose issue code -> the notification method apply_fixes should add
_FIXABLE_METHOD = {"no_email": "email", "no_center": "center"}
# issue codes fixed by rewriting the recording track (mode + pre/post-record)
_RECORDING_CODES = {"rec_not_motion", "pre_record_low", "post_record_low"}


def _get_text(cfg, path):
    _, raw = camera.camera_get(cfg, path)
    return raw.decode("utf-8", "replace")


def _methods(text):
    return re.findall(r"<notificationMethod>(\w+)</notificationMethod>", text)


def _mail_status(cfg):
    for ep in MAILING_EPS:
        try:
            t = _get_text(cfg, ep)
        except Exception:  # noqa: BLE001 - try the next endpoint
            continue
        has_server = bool(re.search(r"<hostName>[^<]+</hostName>", t))
        receivers = len(re.findall(r"<receiverAddress>", t)) or len(re.findall(r"<emailAddress>", t))
        return {"ok": has_server and receivers > 0, "server": has_server,
                "receivers": receivers, "endpoint": ep}
    return {"ok": False, "server": False, "receivers": 0, "endpoint": None}


def _read_track(cfg, track_id):
    """Read a recording track's mode + pre/post-record + top-level Enable. Returns
    {xml, modes, pre, post, enabled} or None if the track isn't readable."""
    try:
        t = _get_text(cfg, RECORD_TRACK.format(tid=track_id))
    except Exception:  # noqa: BLE001 - absent/locked track
        return None
    pre = re.search(r"<PreRecordTimeSeconds>(\d+)</PreRecordTimeSeconds>", t)
    post = re.search(r"<PostRecordTimeSeconds>(\d+)</PostRecordTimeSeconds>", t)
    en = re.search(r"<Enable>(\w+)</Enable>", t)  # first = the Track's own Enable
    return {"xml": t,
            "modes": re.findall(r"<ActionRecordingMode>([^<]+)</ActionRecordingMode>", t),
            "pre": int(pre.group(1)) if pre else None,
            "post": int(post.group(1)) if post else None,
            "enabled": (en.group(1).lower() == "true") if en else None}


def _input_status(cfg):
    """Map input-index(str) -> {"unused": bool} from the video-input list. An input
    is "unused" when it has no camera: videoInputEnabled=false or resDesc=NO VIDEO
    (an empty DVR slot). Missing fields default to in-use, so this never misflags a
    firmware that omits them."""
    try:
        t = _get_text(cfg, VIDEO_INPUTS)
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for blk in re.findall(r"<VideoInputChannel\b.*?</VideoInputChannel>", t, re.S):
        i = re.search(r"<id>(\d+)</id>", blk)
        if not i:
            continue
        en = re.search(r"<videoInputEnabled>(\w+)</videoInputEnabled>", blk)
        res = re.search(r"<resDesc>([^<]*)</resDesc>", blk)
        disabled = bool(en and en.group(1).lower() == "false")
        no_video = bool(res and "NO VIDEO" in res.group(1).strip().upper())
        out[i.group(1)] = {"unused": disabled or no_video}
    return out


def _disable_recording(cfg, track_id):
    """Turn a channel's recording OFF (RMW the track's top-level Enable). Used to
    stop wasting disk on an empty or hidden channel."""
    tr = _read_track(cfg, track_id)
    if not tr:
        return False
    xml = re.sub(r"<Enable>[^<]*</Enable>", "<Enable>false</Enable>", tr["xml"], count=1)
    try:
        camera.camera_put(cfg, RECORD_TRACK.format(tid=track_id), xml.encode("utf-8"))
        return True
    except Exception:  # noqa: BLE001 - surfaces as "not fixed"
        return False


# --- DVR clock -------------------------------------------------------------
def _parse_offset(s):
    """'+03:00' / '-05:30' -> seconds east of UTC."""
    sign = 1 if s[0] == "+" else -1
    hh, mm = s[1:].split(":")
    return sign * (int(hh) * 3600 + int(mm) * 60)


def _read_time(cfg):
    """Read /ISAPI/System/time. Returns {xml, naive, tz, off, local} or None.
    `naive` is the DVR wall-clock (no tz); `off` is its UTC offset in seconds."""
    try:
        t = _get_text(cfg, TIME_EP)
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"<localTime>(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})([+-]\d{2}:\d{2})</localTime>", t)
    if not m:
        return None
    return {"xml": t, "naive": datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"),
            "tz": m.group(2), "off": _parse_offset(m.group(2)), "local": m.group(1) + m.group(2)}


def _correct_local(off):
    """Real wall-clock time in a zone `off` seconds east of UTC (tz-naive)."""
    return (datetime.now(timezone.utc) + timedelta(seconds=off)).replace(tzinfo=None)


def _clock_status(cfg):
    """{ok, dvr_time, offset_secs[, fixable, issue]} — how far the DVR clock drifts
    from real local time (using the DVR's own configured UTC offset)."""
    tr = _read_time(cfg)
    if not tr:
        return {"ok": True, "unknown": True}
    diff = round((tr["naive"] - _correct_local(tr["off"])).total_seconds())
    out = {"ok": abs(diff) <= CLOCK_TOLERANCE, "dvr_time": tr["local"], "offset_secs": diff}
    if not out["ok"]:
        out["fixable"] = True
        out["issue"] = (f"DVR clock is off by ~{round(abs(diff) / 60)} min — recording, OSD and "
                        "e-mail timestamps will be wrong. Set it to the correct local time.")
    return out


def _set_clock(cfg):
    """RMW the DVR clock to the correct local time in its own timezone (keeps
    timeMode/timeZone; only <localTime> changes)."""
    tr = _read_time(cfg)
    if not tr:
        return False
    correct = _correct_local(tr["off"]).strftime("%Y-%m-%dT%H:%M:%S") + tr["tz"]
    xml = re.sub(r"<localTime>[^<]*</localTime>", f"<localTime>{correct}</localTime>", tr["xml"], count=1)
    try:
        camera.camera_put(cfg, TIME_EP, xml.encode("utf-8"))
        return True
    except Exception:  # noqa: BLE001
        return False


def _set_recording(cfg, track_id):
    """Read-modify-write the track: motion recording everywhere + 10s pre/post."""
    tr = _read_track(cfg, track_id)
    if not tr:
        return False
    xml = tr["xml"]
    # only the per-schedule ActionRecordingMode drives recording; DefaultRecordingMode
    # is read-only on this firmware (PUTting a changed value -> 400 badXmlContent).
    xml = re.sub(r"<ActionRecordingMode>[^<]*</ActionRecordingMode>",
                 f"<ActionRecordingMode>{REC_MODE}</ActionRecordingMode>", xml)
    xml = re.sub(r"<PreRecordTimeSeconds>[^<]*</PreRecordTimeSeconds>",
                 f"<PreRecordTimeSeconds>{PREPOST_SECS}</PreRecordTimeSeconds>", xml)
    xml = re.sub(r"<PostRecordTimeSeconds>[^<]*</PostRecordTimeSeconds>",
                 f"<PostRecordTimeSeconds>{PREPOST_SECS}</PostRecordTimeSeconds>", xml)
    try:
        camera.camera_put(cfg, RECORD_TRACK.format(tid=track_id), xml.encode("utf-8"))
        return True
    except Exception:  # noqa: BLE001 - surfaces as "not fixed"
        return False


def _read_stream(cfg, cid):
    """Read the main stream's current resolution. Returns {xml, w, h} or None."""
    try:
        t = _get_text(cfg, STREAM_MAIN.format(cid=cid))
    except Exception:  # noqa: BLE001
        return None
    w = re.search(r"<videoResolutionWidth>(\d+)</videoResolutionWidth>", t)
    h = re.search(r"<videoResolutionHeight>(\d+)</videoResolutionHeight>", t)
    if not (w and h):
        return None
    return {"xml": t, "w": int(w.group(1)), "h": int(h.group(1))}


# Standard resolution pairs, largest first. Capabilities list width/height opts
# separately, so max-of-each can form an invalid pair (e.g. 1920x1536); instead we
# pick the largest KNOWN pair that fits within the advertised width/height bounds.
_STD_RES = [(3840, 2160), (2592, 1944), (2560, 1440), (2048, 1536),
            (1920, 1080), (1280, 960), (1280, 720), (704, 576), (640, 480)]


def _res_opts(cfg, cid):
    """The discrete width/height option sets the stream capabilities advertise.
    (Two sets, not a single max: a value can be listed yet the device still reject
    a given *pair* — see _set_stream_max_res.)"""
    try:
        t = _get_text(cfg, STREAM_MAIN.format(cid=cid) + "/capabilities")
    except Exception:  # noqa: BLE001
        return set(), set()
    wm = re.search(r'<videoResolutionWidth opt="([^"]+)"', t)
    hm = re.search(r'<videoResolutionHeight opt="([^"]+)"', t)
    W = {int(x) for x in re.findall(r"\d+", wm.group(1))} if wm else set()
    H = {int(x) for x in re.findall(r"\d+", hm.group(1))} if hm else set()
    return W, H


def _max_resolution(cfg, cid):
    """The largest *advertised* standard resolution (both width and height in the
    option lists). This is only what capabilities CLAIM — the device may still
    reject it (this DVR advertises 1920x1080 on a 720p-only channel and 500s the
    PUT); the actual write is verified in _set_stream_max_res. Falls back to Full
    HD when capabilities can't be read."""
    W, H = _res_opts(cfg, cid)
    if not W or not H:
        return FULLHD
    for w, h in _STD_RES:
        if w in W and h in H:
            return (w, h)
    return FULLHD


def _set_stream_max_res(cfg, cid):
    """Raise the main stream to the highest resolution the device ACTUALLY accepts.
    Capabilities over-advertise, so we try advertised standard pairs largest-first,
    and only accept one that both PUTs without error AND reads back changed
    (deviceError 500 or a silent revert -> step down and try the next). Returns True
    only when the resolution was genuinely increased."""
    st = _read_stream(cfg, cid)
    if not st:
        return False
    W, H = _res_opts(cfg, cid)
    cur_area = st["w"] * st["h"]
    for w, h in _STD_RES:                     # largest-first
        if w * h <= cur_area:
            break                             # nothing bigger than current remains
        if W and H and (w not in W or h not in H):
            continue                          # not advertised -> don't even try (avoid needless failed PUTs)
        xml = re.sub(r"<videoResolutionWidth>\d+</videoResolutionWidth>",
                     f"<videoResolutionWidth>{w}</videoResolutionWidth>", st["xml"], count=1)
        xml = re.sub(r"<videoResolutionHeight>\d+</videoResolutionHeight>",
                     f"<videoResolutionHeight>{h}</videoResolutionHeight>", xml, count=1)
        try:
            camera.camera_put(cfg, STREAM_MAIN.format(cid=cid), xml.encode("utf-8"))
        except Exception:  # noqa: BLE001 - device rejected this resolution; try a smaller one
            continue
        rb = _read_stream(cfg, cid)
        if rb and rb["w"] == w and rb["h"] == h:
            return True                       # verified it stuck
    return False


def _add_quality_issue(cfg, ch, rep):
    """Flag recording below HD. We DON'T flag 'below advertised max': this DVR
    over-advertises (a 720p channel lists 1080p), so nagging to raise it would loop
    forever on a PUT the device 500s. 720p is HD and an acceptable ceiling; only
    genuinely sub-HD (D1/CIF) recording is worth fixing — and the fix raises it to
    the highest resolution the device truly accepts (see _set_stream_max_res)."""
    st = _read_stream(cfg, ch["id"])
    if not st:
        return
    mx = _max_resolution(cfg, ch["id"])
    rep["rec_resolution"] = f"{st['w']}x{st['h']}"
    rep["max_resolution"] = f"{mx[0]}x{mx[1]}"
    if st["h"] < MIN_HD_HEIGHT:
        rep["issues"].append({"code": "rec_quality_low", "fixable": True,
                              "msg": f"Recording below HD — main stream is {st['w']}x{st['h']}; "
                                     f"raising to the camera's max ({mx[0]}x{mx[1]})."})


def _add_recording_issues(cfg, ch, rep):
    """Check the channel's recording track for motion mode + 10s pre/post-record."""
    tr = _read_track(cfg, ch["id"])
    if not tr or not tr["modes"]:
        return  # can't read the track -> don't report recording checks for it
    all_motion = all(m == REC_MODE for m in tr["modes"])
    rep["record_mode"] = "motion" if all_motion else ",".join(sorted(set(tr["modes"]))).lower()
    rep["pre_record"] = tr["pre"]
    rep["post_record"] = tr["post"]
    if not all_motion:
        rep["issues"].append({"code": "rec_not_motion", "fixable": True,
                              "msg": "Recording isn't motion-triggered — no per-motion clips to log."})
    if (tr["pre"] or 0) < PREPOST_SECS:
        rep["issues"].append({"code": "pre_record_low", "fixable": True,
                              "msg": f"Pre-record < {PREPOST_SECS}s — clips won't start before motion."})
    if (tr["post"] or 0) < PREPOST_SECS:
        rep["issues"].append({"code": "post_record_low", "fixable": True,
                              "msg": f"Post-record < {PREPOST_SECS}s — clips won't run after motion."})


def _channel_report(cfg, ch):
    rep = {"input": ch["input"], "id": ch["id"], "name": ch["name"],
           "reachable": True, "issues": []}
    try:
        mt = _get_text(cfg, MOTION_EP.format(n=ch["input"]))
    except Exception as e:  # noqa: BLE001 - a disabled/absent input 403s here
        rep["reachable"] = False
        rep["detail"] = type(e).__name__
        return rep

    en = re.search(r"<enabled>(\w+)</enabled>", mt)
    rep["motion_enabled"] = bool(en and en.group(1) == "true")
    g = re.search(r"<gridMap>([0-9a-fA-F]*)</gridMap>", mt)
    cols = int((re.search(r"<columnGranularity>(\d+)", mt) or [0, 22])[1])
    rows = int((re.search(r"<rowGranularity>(\d+)", mt) or [0, 18])[1])
    cells = sum(v for row in motion.decode_gridmap(g.group(1), cols, rows) for v in row) if g else 0
    rep["area_painted"] = cells > 0
    s = re.search(r"<sensitivityLevel>(\d+)</sensitivityLevel>", mt)
    rep["sensitivity"] = int(s.group(1)) if s else None

    try:
        methods = _methods(_get_text(cfg, VMD_TRIGGER.format(n=ch["input"])))
    except Exception:  # noqa: BLE001 - no trigger -> treat as unlinked
        methods = []
    rep["email_linked"] = "email" in methods
    rep["center_linked"] = "center" in methods

    if not rep["motion_enabled"]:
        rep["issues"].append({"code": "motion_disabled", "fixable": False,
                              "msg": "Motion detection is OFF — enable it and paint an area (⚙ → Motion detection area)."})
    elif not rep["area_painted"]:
        rep["issues"].append({"code": "no_area", "fixable": False,
                              "msg": "No detection area painted — open ⚙ → Motion detection area and paint the zone."})
    if not rep["email_linked"]:
        rep["issues"].append({"code": "no_email", "fixable": True,
                              "msg": "Won't e-mail on motion — 'email' linkage missing on the VMD trigger."})
    if not rep["center_linked"]:
        rep["issues"].append({"code": "no_center", "fixable": True,
                              "msg": "App won't show motion — 'Notify Surveillance Center' linkage missing (feeds the alert stream)."})
    _add_recording_issues(cfg, ch, rep)
    _add_quality_issue(cfg, ch, rep)
    return rep


def _unused_channel_report(cfg, ch, reason):
    """A camera-less ('no_camera') or user-hidden ('hidden') channel: skip the
    motion/linkage/quality checks and only flag recording left ON (wastes space)."""
    rep = {"input": ch["input"], "id": ch["id"], "name": ch["name"],
           "reachable": True, "unused": reason, "issues": []}
    tr = _read_track(cfg, ch["id"])
    rep["recording_on"] = bool(tr and tr["enabled"])
    if rep["recording_on"]:
        why = "has no camera (NO VIDEO)" if reason == "no_camera" else "is hidden from the app"
        rep["issues"].append({"code": "rec_wasteful", "fixable": True,
                              "msg": f"This channel {why} but the DVR is still recording it — "
                                     "turn recording off to save disk space."})
    return rep


def diagnose(cfg, hidden=()):
    """Full per-device report: {smtp, clock, channels[], fixable}. Channels the
    user hid, or inputs with no camera (NO VIDEO), get the lightweight unused-channel
    check (recording-left-on only) instead of the full motion/quality diagnosis."""
    hidden = set(hidden or ())
    inputs = _input_status(cfg)
    channels = []
    for ch in camera.discover_channels(cfg):
        if ch["id"] in hidden:
            channels.append(_unused_channel_report(cfg, ch, "hidden"))
        elif inputs.get(str(ch["input"]), {}).get("unused"):
            channels.append(_unused_channel_report(cfg, ch, "no_camera"))
        else:
            channels.append(_channel_report(cfg, ch))
    smtp = _mail_status(cfg)
    # an email linkage is useless without SMTP — flag it once at the device level
    any_email = any(c.get("email_linked") for c in channels)
    if any_email and not smtp["ok"]:
        smtp["issue"] = ("SMTP not configured (server + recipient) — motion e-mail "
                         "can't be sent. Set it on the DVR's Email page.")
    clock = _clock_status(cfg)
    fixable = clock.get("fixable", False) or any(i["fixable"] for c in channels for i in c["issues"])
    return {"smtp": smtp, "clock": clock, "channels": channels, "fixable": fixable}


def _add_methods(cfg, input_id, want):
    """RMW: ensure each notification method in `want` is present on VMD-<input>,
    preserving the existing linkages. Returns the methods actually added."""
    path = VMD_TRIGGER.format(n=input_id)
    text = _get_text(cfg, path)
    close = "</EventTriggerNotificationList>"
    i = text.find(close)
    if i == -1:
        return []
    insert, added = "", []
    for m in want:
        if f"<notificationMethod>{m}</notificationMethod>" not in text:
            insert += (f"<EventTriggerNotification>\n<id>{m}</id>\n"
                       f"<notificationMethod>{m}</notificationMethod>\n</EventTriggerNotification>\n")
            added.append(m)
    if not insert:
        return []
    camera.camera_put(cfg, path, (text[:i] + insert + text[i:]).encode("utf-8"))
    return added


def apply_fixes(cfg, hidden=()):
    """Apply every auto-fixable repair diagnose() found — linkage, recording
    mode/pre-post/resolution, recording-off for empty/hidden channels, and the DVR
    clock — then re-diagnose. Returns {fixes:[{input,name,added}], report}."""
    fixes = []
    rep = diagnose(cfg, hidden)
    for c in rep["channels"]:
        added = []
        want = [_FIXABLE_METHOD[i["code"]] for i in c["issues"]
                if i.get("fixable") and i["code"] in _FIXABLE_METHOD]
        if want:
            added += _add_methods(cfg, c["input"], want)
        if any(i["code"] in _RECORDING_CODES for i in c["issues"]) and _set_recording(cfg, c["id"]):
            added.append(f"motion-rec {PREPOST_SECS}s/{PREPOST_SECS}s")
        if any(i["code"] == "rec_quality_low" for i in c["issues"]) and _set_stream_max_res(cfg, c["id"]):
            added.append("max-resolution")
        if any(i["code"] == "rec_wasteful" for i in c["issues"]) and _disable_recording(cfg, c["id"]):
            added.append("recording off (unused channel)")
        if added:
            fixes.append({"input": c["input"], "name": c["name"], "added": added})
    if rep["clock"].get("fixable") and _set_clock(cfg):
        fixes.append({"input": "—", "name": "DVR clock", "added": ["set correct local time"]})
    return {"fixes": fixes, "report": diagnose(cfg, hidden)}
