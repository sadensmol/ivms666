"""Health check for a DVR's motion -> notification pipeline, plus a safe auto-fix
for the linkage gaps it can repair.

Per video-input channel it checks:
  - motion detection enabled + an area actually painted (cellsOn > 0)
  - the VMD event trigger links `email`  (so the DVR can e-mail on motion)
  - the VMD event trigger links `center` (so the event reaches the ISAPI alert
    stream that drives this app's live motion indicator — see the CLAUDE.md
    gotcha; without `center`, `record`/`email` still fire but the app never sees
    the event)
and device-wide:
  - SMTP/mailing is configured (a server + at least one recipient)

Auto-fixable: a missing `email`/`center` on a VMD trigger, added via a
read-modify-write that preserves the existing linkages. Not auto-fixable (reported
with guidance instead): motion disabled, no area painted, SMTP unconfigured — those
need the Motion editor or the DVR's own Email page.
"""

import re

from . import camera, motion

VMD_TRIGGER = "/ISAPI/Event/triggers/VMD-{n}"
MOTION_EP = "/ISAPI/System/Video/inputs/channels/{n}/motionDetection"
MAILING_EPS = ("/ISAPI/System/Network/mailing", "/ISAPI/System/Network/mailing/1")

# diagnose issue code -> the notification method apply_fixes should add
_FIXABLE_METHOD = {"no_email": "email", "no_center": "center"}


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
    return rep


def diagnose(cfg, hidden=()):
    """Full per-device report: {smtp, channels[], fixable}. Channels whose id is
    in `hidden` (tiles the user removed from view) are skipped."""
    hidden = set(hidden or ())
    chans = [ch for ch in camera.discover_channels(cfg) if ch["id"] not in hidden]
    channels = [_channel_report(cfg, ch) for ch in chans]
    smtp = _mail_status(cfg)
    # an email linkage is useless without SMTP — flag it once at the device level
    any_email = any(c.get("email_linked") for c in channels)
    if any_email and not smtp["ok"]:
        smtp["issue"] = ("SMTP not configured (server + recipient) — motion e-mail "
                         "can't be sent. Set it on the DVR's Email page.")
    fixable = any(i["fixable"] for c in channels for i in c["issues"])
    return {"smtp": smtp, "channels": channels, "fixable": fixable}


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
    """Apply every auto-fixable linkage repair diagnose() found (skipping hidden
    channels), then re-diagnose. Returns {fixes:[{input,name,added}], report}."""
    fixes = []
    for c in diagnose(cfg, hidden)["channels"]:
        want = [_FIXABLE_METHOD[i["code"]] for i in c["issues"]
                if i.get("fixable") and i["code"] in _FIXABLE_METHOD]
        if want:
            added = _add_methods(cfg, c["input"], want)
            if added:
                fixes.append({"input": c["input"], "name": c["name"], "added": added})
    return {"fixes": fixes, "report": diagnose(cfg, hidden)}
