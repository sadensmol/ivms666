"""ISAPI camera access (Digest with Basic fallback) and channel discovery.

Pure standard library. Functions take a `cfg` dict: {host, port, user, password}.
"""

import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

# The device's picture endpoint caps at 720p — it rejects 1080p — so this is the
# "max resolution" still we can save (see CLAUDE.md). fetch_snapshot falls back
# to the device default if even this is refused.
MAX_STILL_RES = "1280x720"


def _opener(cfg, url):
    pwmgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    pwmgr.add_password(None, url, cfg.get("user", ""), cfg.get("password", ""))
    return urllib.request.build_opener(
        urllib.request.HTTPDigestAuthHandler(pwmgr),
        urllib.request.HTTPBasicAuthHandler(pwmgr),
    )


def camera_get(cfg, path, timeout=15):
    """GET a path on the camera. Returns (content_type, bytes). Raises on error."""
    if not path.startswith("/"):
        path = "/" + path
    url = f"http://{cfg['host']}:{cfg['port']}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "camera-viewer"})
    with _opener(cfg, url).open(req, timeout=timeout) as resp:
        return resp.headers.get("Content-Type", "application/octet-stream"), resp.read()


def camera_put(cfg, path, body, timeout=15):
    """PUT xml to a path on the camera. Returns (content_type, bytes)."""
    if not path.startswith("/"):
        path = "/" + path
    url = f"http://{cfg['host']}:{cfg['port']}{path}"
    req = urllib.request.Request(
        url, data=body, method="PUT",
        headers={"User-Agent": "camera-viewer", "Content-Type": "application/xml"},
    )
    with _opener(cfg, url).open(req, timeout=timeout) as resp:
        return resp.headers.get("Content-Type", ""), resp.read()


def open_stream(cfg, path, timeout=60):
    """Open a long-lived GET (e.g. the ISAPI event alert stream) and return the
    raw response object for incremental `.read()`. Digest/Basic auth handled as
    usual; the caller must `.close()` it. `timeout` is the per-read socket
    timeout — a silent stream past it raises and the caller reconnects."""
    if not path.startswith("/"):
        path = "/" + path
    url = f"http://{cfg['host']}:{cfg['port']}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "camera-viewer"})
    return _opener(cfg, url).open(req, timeout=timeout)


def camera_post(cfg, path, body, timeout=30):
    """POST xml to a path on the camera (e.g. ContentMgmt/search). Returns
    (content_type, bytes)."""
    if not path.startswith("/"):
        path = "/" + path
    url = f"http://{cfg['host']}:{cfg['port']}{path}"
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"User-Agent": "camera-viewer", "Content-Type": "application/xml"},
    )
    with _opener(cfg, url).open(req, timeout=timeout) as resp:
        return resp.headers.get("Content-Type", ""), resp.read()


def fetch_snapshot(cfg, channel_id, resolution=None):
    """Fetch a JPEG still. `resolution` like '1280x720' asks the DVR for a
    higher-quality frame (the endpoint defaults to a low D1 image); if the
    device rejects that size we transparently fall back to its default."""
    base = f"/ISAPI/Streaming/channels/{channel_id}/picture"
    if resolution:
        try:
            w, h = (int(x) for x in resolution.lower().split("x"))
            return camera_get(cfg, f"{base}?videoResolutionWidth={w}&videoResolutionHeight={h}")
        except Exception:
            pass  # unsupported size / parse error -> device default below
    return camera_get(cfg, base)


def reboot(cfg):
    """Reboot the whole device (Hikvision ISAPI `PUT /ISAPI/System/reboot`, empty
    body). This drops every channel/stream for ~a minute. Returns (ok, message)."""
    _, resp = camera_put(cfg, "/ISAPI/System/reboot", b"")
    body = resp.decode("utf-8", "replace")
    ok = ("<statusString>OK" in body) or ("statusCode>1<" in body) or body.strip() == ""
    return ok, body[:400]


def _localname(tag):
    return tag.rsplit("}", 1)[-1]


def parse_xml(raw):
    """Safely parse camera XML with stdlib only.

    Reject any DOCTYPE/ENTITY declaration up front so the expat parser can
    never be driven into external-entity (XXE) or billion-laughs expansion —
    both require an entity/DOCTYPE declaration, which a normal ISAPI response
    never contains.
    """
    low = raw.lower() if isinstance(raw, bytes) else raw.lower().encode()
    if b"<!doctype" in low or b"<!entity" in low:
        raise ValueError("refusing XML with DOCTYPE/ENTITY declarations")
    return ET.fromstring(raw)


def discover_channels(cfg):
    """Return [{'id': '101', 'input': '1', 'name': 'Camera 01'}, ...].

    'id' is the streaming/picture channel id; 'input' is the physical video
    input index used by the motionDetection endpoint. Tries ISAPI channel
    listings first, then falls back to probing channels 1..16.
    """
    # 1) Physical video input channels -> picture id is "<n>01".
    try:
        _, raw = camera_get(cfg, "/ISAPI/System/Video/inputs/channels")
        found = []
        for node in parse_xml(raw).iter():
            if _localname(node.tag) != "VideoInputChannel":
                continue
            cid = name = None
            for child in node:
                ln = _localname(child.tag)
                if ln == "id":
                    cid = (child.text or "").strip()
                elif ln == "name":
                    name = (child.text or "").strip()
            if cid:
                found.append({"id": f"{cid}01", "input": cid, "name": name or f"Channel {cid}"})
        if found:
            return found
    except Exception:
        pass

    # 2) Streaming channels -> keep main streams (ids ending in "01").
    try:
        _, raw = camera_get(cfg, "/ISAPI/Streaming/channels")
        found = []
        for node in parse_xml(raw).iter():
            if _localname(node.tag) != "StreamingChannel":
                continue
            cid = name = None
            for child in node:
                ln = _localname(child.tag)
                if ln == "id":
                    cid = (child.text or "").strip()
                elif ln == "channelName":
                    name = (child.text or "").strip()
            if cid and cid.endswith("01"):
                found.append({"id": cid, "input": str(int(cid) // 100),
                              "name": name or f"Channel {cid}"})
        if found:
            return found
    except Exception:
        pass

    # 3) Fallback: probe channels 1..16 by trying to grab a still.
    found = []
    for n in range(1, 17):
        cid = f"{n}01"
        try:
            fetch_snapshot(cfg, cid)
            found.append({"id": cid, "input": str(n), "name": f"Channel {n}"})
        except Exception:
            pass
    return found
