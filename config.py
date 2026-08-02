"""Device store and persistence.

Devices (host/port/credentials + per-device hidden-tile setup) are persisted to
CONFIG_PATH as JSON, chmod 0600. All access goes through the thread-safe helpers
below; passwords never leave the server (see `mask`).
"""

import json
import os
import re
import secrets
import threading
from urllib.parse import unquote

AGENTGREEN_PORT = "8090"   # default management port for "agentgreen" devices


def _env_port(name, default):
    """Read a port from the environment; a junk value falls back, never crashes."""
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


# Bind address is env-overridable because the same code runs two ways: locally it
# must stay on loopback (the app has no auth of its own), while in a container it
# has to listen on 0.0.0.0 for the tunnel sidecar to reach it. The DEFAULT stays
# loopback so nothing is ever exposed by accident.
LISTEN_HOST = os.environ.get("CV_LISTEN_HOST") or "127.0.0.1"
LISTEN_PORT = _env_port("CV_LISTEN_PORT", 8777)
# HOME-relative, so pointing HOME at a mounted volume persists config + saves.
CONFIG_PATH = os.path.expanduser("~/.ivms666.json")
# Pre-rename location; `load` migrates it once so an existing install keeps its
# devices (and its credentials) without a manual move.
LEGACY_CONFIG_PATH = os.path.expanduser("~/.camera_viewer.json")
# Where snapshots (motion auto-captures + manual saves) are written by default.
DEFAULT_SAVE_PATH = os.path.expanduser("~/ivms666")
# Bundled, secret-free defaults shipped with the app (no host/creds — the live
# config with secrets lives only in CONFIG_PATH, 0600, outside the repo).
DEFAULTS_PATH = os.path.join(os.path.dirname(__file__), "default_config.json")


def _load_defaults():
    """Read the bundled default config; fall back to a safe literal if missing."""
    try:
        with open(DEFAULTS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError) as e:
        print(f"Warning: could not read {DEFAULTS_PATH}: {e}")
    return {"devices": [], "scan": {"range": "", "ports": "554"}}


_DEFAULTS = _load_defaults()

# {"devices": [{id, name, host, port, user, password, hidden:[ids]}],
#  "scan": {"range": "<ip range for rtsp-scan>", "ports": "554"}}
_state = {"devices": list(_DEFAULTS.get("devices") or []),
          "groups": list(_DEFAULTS.get("groups") or [])}
_lock = threading.Lock()


def _migrate_legacy():
    """Adopt a pre-rename ~/.camera_viewer.json, once.

    Copy rather than rename: the old file keeps working as a fallback if the
    rename turns out to be a mistake. Only ever runs when the new path does not
    exist yet, so it can never clobber current config.
    """
    if os.path.exists(CONFIG_PATH) or not os.path.exists(LEGACY_CONFIG_PATH):
        return
    try:
        with open(LEGACY_CONFIG_PATH, "rb") as src:
            data = src.read()
        fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as dst:
            dst.write(data)
        print(f"Migrated {LEGACY_CONFIG_PATH} -> {CONFIG_PATH}")
    except OSError as e:
        print(f"Warning: could not migrate {LEGACY_CONFIG_PATH}: {e}")


def load():
    """Load persisted state from CONFIG_PATH (no-op if the file is absent)."""
    global _state
    _migrate_legacy()
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("devices"), list):
            _state = data
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        print(f"Warning: could not read {CONFIG_PATH}: {e}")


def _save_locked():
    """Atomically write state and restrict it to the current user. Caller holds _lock."""
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_state, f, indent=2)
    os.replace(tmp, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def _find(device_id):
    return next((d for d in _state["devices"] if d.get("id") == device_id), None)


def is_isapi(d):
    """Whether the device talks ISAPI (a DVR). Explicit `isapi_enabled` wins;
    otherwise a kind=rtsp / legacy rtsp_url device defaults to OFF, else ON."""
    if "isapi_enabled" in d:
        return bool(d["isapi_enabled"])
    return not (d.get("kind") == "rtsp" or bool(d.get("rtsp_url")))


def is_rtsp(d):
    """True for an RTSP-only device (ISAPI off / no ISAPI)."""
    return not is_isapi(d)


def _rtsp_path(d):
    """The RTSP path (camera) for an RTSP-only device — the stored `path`, or the
    path parsed from a legacy `rtsp_url`."""
    if d.get("path"):
        return d["path"]
    if d.get("rtsp_url"):
        p = _parse_rtsp_url(d["rtsp_url"])
        if p:
            return p["path"]
    return "/"


def strip_url_creds(url):
    """'rtsp://user:pass@host:port/path' -> 'rtsp://host:port/path' (never show the
    password to the browser). `[^/]*@` is greedy up to the LAST '@' in the authority,
    so a password containing '@', ':' or '#' is still stripped whole (userinfo ends
    at the last '@' before the path — RFC 3986)."""
    return re.sub(r"^([a-zA-Z][\w+.-]*://)[^/]*@", r"\1", url or "")


def device_cfg(d):
    """The subset a camera request needs (includes the password — server-side only)."""
    cfg = {"host": d.get("host", ""), "port": str(d.get("port") or "80"),
           "user": d.get("user", ""), "password": d.get("password", ""),
           "rtsp_port": str(d.get("rtsp_port") or "554"),
           "agentgreen_port": str(d.get("agentgreen_port") or AGENTGREEN_PORT),
           "isapi_enabled": is_isapi(d)}
    if is_rtsp(d):
        cfg["kind"] = "rtsp"
        cfg["path"] = _rtsp_path(d)
        cfg["name"] = d.get("name", "")
        if d.get("rtsp_url"):
            cfg["rtsp_url"] = d["rtsp_url"]   # legacy: compose fallback in live.rtsp_url
    return cfg


def mask(d):
    """Browser-safe device view — never leaks the stored password or URL creds."""
    view = {"id": d["id"], "name": d.get("name") or d.get("host") or "device",
            "kind": "dvr" if is_isapi(d) else "rtsp", "host": d.get("host", ""),
            "port": str(d.get("port") or "80"), "isapiEnabled": is_isapi(d),
            "rtspPort": str(d.get("rtsp_port") or "554"),
            "agentgreenPort": str(d.get("agentgreen_port") or AGENTGREEN_PORT),
            "agentgreenEnabled": bool(d.get("agentgreen_enabled", False)),
            "user": d.get("user", ""), "hasPassword": bool(d.get("password")),
            "group": d.get("group", ""), "hidden": d.get("hidden", []),
            "inactive": d.get("inactive", []),
            "audioOnly": bool(d.get("audio_only", False))}
    if is_rtsp(d):
        view["path"] = _rtsp_path(d)   # the RTSP camera path (verbatim from the URL)
    return view


# --- thread-safe public API -------------------------------------------------
def list_devices():
    with _lock:
        return [mask(d) for d in _state["devices"]]


def get_cfg(device_id):
    """Return a camera-config dict for the device, or None if unknown."""
    with _lock:
        d = _find(device_id)
        return device_cfg(d) if d else None


def _to_bool(v, default=False):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _parse_rtsp_url(url):
    """rtsp://[user[:pass]@]host[:port]/path -> {user,password,host,rtsp_port,path}
    or None. Userinfo ends at the LAST '@' before the path, so a password with
    '@'/':' parses correctly. `path` (whatever it is — NOT assumed to be
    /Streaming/Channels/<id>) is kept verbatim and used as the camera + in the URL."""
    m = re.match(r"^rtsp://(.*)$", (url or "").strip(), re.I)
    if not m:
        return None
    rest = m.group(1)
    slash = rest.find("/")
    authority = rest if slash == -1 else rest[:slash]
    path = "/" if slash == -1 else rest[slash:]
    user = pw = ""
    at = authority.rfind("@")
    if at != -1:
        user, _, pw = authority[:at].partition(":")
        authority = authority[at + 1:]
    hm = re.match(r"^([^:/]+)(?::(\d+))?$", authority)
    if not hm:
        return None
    return {"user": unquote(user), "password": unquote(pw), "host": hm.group(1),
            "rtsp_port": hm.group(2) or "554", "path": path or "/"}


def add_device(fields):
    """Create a device from user-supplied fields; returns the masked view. An
    `rtsp_url` makes an RTSP-only device: the URL is PARSED into the standard fields
    (host/rtsp_port/user/password + `path`) — no opaque URL is stored, so every
    field is editable in the normal dialog. `path` is the camera and its default
    name."""
    if fields.get("rtsp_url"):
        p = _parse_rtsp_url(str(fields["rtsp_url"]))
        if not p:
            raise ValueError("not a valid rtsp:// URL")
        device = {
            "id": secrets.token_hex(4),
            "name": str(fields.get("name") or "").strip() or (p["host"] + p["path"]),
            "kind": "rtsp",
            "isapi_enabled": False,        # RTSP-only: no ISAPI
            "host": p["host"],
            "port": str(fields.get("port") or "80").strip(),
            "rtsp_port": p["rtsp_port"],
            "agentgreen_port": str(fields.get("agentgreen_port") or AGENTGREEN_PORT).strip(),
            "user": p["user"],
            "password": p["password"],
            "path": p["path"],
            "hidden": [],
        }
    else:
        device = {
            "id": secrets.token_hex(4),
            "name": str(fields.get("name") or "").strip(),
            "kind": "dvr",
            "isapi_enabled": _to_bool(fields.get("isapi_enabled"), True),
            "host": str(fields["host"]).strip(),
            "port": str(fields["port"]).strip(),
            "rtsp_port": str(fields.get("rtsp_port") or "554"),
            "agentgreen_port": str(fields.get("agentgreen_port") or AGENTGREEN_PORT).strip(),
            "user": str(fields.get("user") or ""),
            "password": str(fields.get("password") or ""),
            "hidden": [],
        }
        if fields.get("path"):   # ISAPI off on a DVR form -> an RTSP path to stream
            device["path"] = str(fields["path"]).strip()
    device["group"] = str(fields.get("group") or "").strip()  # optional group membership (any kind)
    device["agentgreen_enabled"] = _to_bool(fields.get("agentgreen_enabled"), False)  # off by default
    with _lock:
        _state["devices"].append(device)
        _save_locked()
    return mask(device)


def update_device(device_id, fields):
    """Apply partial updates; returns the masked view, or None if unknown.

    An empty/absent password is left unchanged (only overwritten when provided).
    """
    with _lock:
        d = _find(device_id)
        if not d:
            return None
        if fields.get("rtsp_url"):                # re-paste a URL -> re-parse into fields
            p = _parse_rtsp_url(str(fields["rtsp_url"]))
            if p:
                d.update({"host": p["host"], "rtsp_port": p["rtsp_port"], "user": p["user"],
                          "password": p["password"], "path": p["path"]})
                d.pop("rtsp_url", None)           # migrate a legacy opaque URL off
        for key in ("name", "host", "port", "user", "rtsp_port", "agentgreen_port", "path"):
            if key in fields and fields[key] is not None:
                d[key] = str(fields[key]) if key == "user" else str(fields[key]).strip()
        if "isapi_enabled" in fields:
            d["isapi_enabled"] = _to_bool(fields["isapi_enabled"], True)
        if "agentgreen_enabled" in fields:
            d["agentgreen_enabled"] = _to_bool(fields["agentgreen_enabled"], False)
        if "audio_only" in fields:   # stream has no video track (set at runtime; reverts if video returns)
            d["audio_only"] = _to_bool(fields["audio_only"], False)
        if fields.get("password"):
            d["password"] = str(fields["password"])
        if isinstance(fields.get("hidden"), list):
            d["hidden"] = [str(x) for x in fields["hidden"]]
        if isinstance(fields.get("inactive"), list):  # paused tiles (not refreshed)
            d["inactive"] = [str(x) for x in fields["inactive"]]
        if "group" in fields and fields["group"] is not None:  # "" removes it from its group
            d["group"] = str(fields["group"]).strip()
        _save_locked()
        return mask(d)


def delete_device(device_id):
    """Remove a device; returns True if one was removed."""
    with _lock:
        before = len(_state["devices"])
        _state["devices"] = [d for d in _state["devices"] if d.get("id") != device_id]
        changed = len(_state["devices"]) != before
        if changed:
            _save_locked()
        return changed


def device_label(device_id):
    """A human-friendly label (name, else host) for filenames/logging."""
    with _lock:
        d = _find(device_id)
        return (d.get("name") or d.get("host")) if d else "camera"


# --- groups (named containers holding devices; may be empty) ----------------
def list_groups():
    """All group names: the persisted list plus any a device still references
    (so a group is never lost if the two drift). Sorted, unique."""
    with _lock:
        names = set(_state.get("groups") or [])
        names |= {str(d.get("group") or "").strip() for d in _state["devices"]
                  if str(d.get("group") or "").strip()}
        return sorted(names)


def create_group(name):
    """Add an (initially empty) group; returns the current group list."""
    name = str(name or "").strip()
    with _lock:
        groups = list(_state.get("groups") or [])
        if name and name not in groups:
            groups.append(name)
            _state["groups"] = groups
            _save_locked()
    return list_groups()


def delete_group(name, delete_devices=False):
    """Remove a group. By default its devices stay but become ungrouped; with
    `delete_devices=True` every member device is removed too. Returns the list of
    deleted device ids (empty unless cascading), so the caller can retire their
    motion monitors."""
    name = str(name or "").strip()
    deleted = []
    with _lock:
        _state["groups"] = [g for g in (_state.get("groups") or []) if g != name]
        members = [d for d in _state["devices"] if str(d.get("group") or "").strip() == name]
        if delete_devices:
            ids = {d.get("id") for d in members}
            deleted = [d["id"] for d in members]
            _state["devices"] = [d for d in _state["devices"] if d.get("id") not in ids]
        else:
            for d in members:
                d["group"] = ""
        _save_locked()
    return deleted


def get_hidden(device_id):
    """The device's hidden-channel ids (tiles the user removed from view)."""
    with _lock:
        d = _find(device_id)
        return list(d.get("hidden", [])) if d else []


# --- app settings (image save path) ----------------------------------------
def _settings_view(s):
    """The effective settings dict browsers see (with defaults filled in)."""
    return {"save_path": s.get("save_path") or DEFAULT_SAVE_PATH,
            "motion_popup": bool(s.get("motion_popup", False))}  # default OFF


def get_settings():
    """App-wide settings: `save_path` (where snapshots land) and `motion_popup`
    (show the full-screen popup when motion fires; off by default)."""
    with _lock:
        return _settings_view(_state.get("settings") or {})


def update_settings(patch):
    """Apply partial settings updates and persist. `save_path` is expanded
    (`~`) and normalized to an absolute path; `motion_popup` is coerced to bool.
    Returns the effective settings."""
    with _lock:
        s = dict(_state.get("settings") or {})
        if patch.get("save_path"):
            s["save_path"] = os.path.abspath(os.path.expanduser(str(patch["save_path"]).strip()))
        if "motion_popup" in patch:
            s["motion_popup"] = bool(patch["motion_popup"])
        _state["settings"] = s
        _save_locked()
        return _settings_view(s)


# --- device file import/export (rtsp-scan -> app setup) ---------------------
def write_devices_file(path, entries):
    """Write a list of device field dicts as import-ready {"devices":[...]} JSON.
    Used by `rtsp-scan --output` so a scan's verified hits can seed the app."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"devices": entries}, f, indent=2)


def read_devices_file(path):
    """Read a device list from an rtsp-scan output file. Accepts either the
    {"devices":[...]} shape or a bare top-level list. Missing/blank/invalid file
    -> []."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    devs = data.get("devices") if isinstance(data, dict) else data
    return [d for d in devs if isinstance(d, dict)] if isinstance(devs, list) else []


def _entry_key(e):
    """Dedup key for a scan-output entry: the rtsp_url (new shape), else
    host+rtsp_port+user (legacy shape)."""
    if e.get("rtsp_url"):
        return ("url", str(e["rtsp_url"]).strip())
    return ("dvr", str(e.get("host") or ""), str(e.get("rtsp_port") or "554"), str(e.get("user") or ""))


def merge_devices_file(path, new_entries):
    """APPEND scan entries to the output file without deleting what's already there
    (dedup by rtsp_url / host+port+user). Creates the file if missing. Returns the
    full merged list. `rtsp-scan` calls this the instant each credential is verified,
    so the file is up to date mid-run and a later run adds to it."""
    merged = read_devices_file(path)
    seen = {_entry_key(e) for e in merged}
    for e in new_entries:
        k = _entry_key(e)
        if k not in seen:
            seen.add(k)
            merged.append(e)
    write_devices_file(path, merged)
    return merged


def _device_key(d):
    """Idempotency key for an in-store device (matches _entry_key on rtsp devices)."""
    if is_rtsp(d):
        return ("url", strip_url_creds(_compose_or_url(d)))
    return ("dvr", str(d.get("host") or ""), str(d.get("rtsp_port") or "554"), str(d.get("user") or ""))


def _compose_or_url(d):
    """The device's rtsp URL (legacy `rtsp_url`, else composed from its fields)."""
    if d.get("rtsp_url"):
        return d["rtsp_url"]
    user = d.get("user") or ""
    from urllib.parse import quote
    cred = f"{quote(user, safe='')}:{quote(d.get('password') or '', safe='')}@" if user else ""
    return f"rtsp://{cred}{d.get('host','')}:{d.get('rtsp_port') or '554'}{_rtsp_path(d)}"


def import_devices(entries):
    """Add devices from a scan output file into the store. Each `rtsp_url` entry is
    PARSED into an RTSP-only device (host/rtsp_port/user/password/path); a legacy
    {host,port,…} entry becomes a DVR. Skips ones duplicating an existing device
    (by rtsp_url / host+rtsp_port+user) so re-importing is idempotent. Returns
    (added, skipped)."""
    added = skipped = 0
    with _lock:
        seen = {_device_key(d) for d in _state["devices"]}
        for e in entries:
            if e.get("rtsp_url"):
                p = _parse_rtsp_url(str(e["rtsp_url"]))
                if not p:
                    skipped += 1
                    continue
                device = {
                    "id": secrets.token_hex(4),
                    "name": str(e.get("name") or "").strip() or (p["host"] + p["path"]),
                    "kind": "rtsp", "isapi_enabled": False,
                    "host": p["host"], "port": "80", "rtsp_port": p["rtsp_port"],
                    "agentgreen_port": AGENTGREEN_PORT, "user": p["user"],
                    "password": p["password"], "path": p["path"],
                    "group": str(e.get("group") or "").strip(), "hidden": [],
                }
            else:
                host = str(e.get("host") or "").strip()
                if not host:
                    skipped += 1
                    continue
                device = {
                    "id": secrets.token_hex(4),
                    "name": str(e.get("name") or "").strip(),
                    "kind": "dvr", "isapi_enabled": True,
                    "host": host, "port": str(e.get("port") or "80").strip(),
                    "user": str(e.get("user") or ""), "password": str(e.get("password") or ""),
                    "rtsp_port": str(e.get("rtsp_port") or "554"),
                    "group": str(e.get("group") or "").strip(), "hidden": [],
                }
            k = _device_key(device)
            if k in seen:
                skipped += 1
                continue
            seen.add(k)
            _state["devices"].append(device)
            added += 1
        if added:
            _save_locked()
    return added, skipped


# --- rtsp-scan config -------------------------------------------------------
def _str_list(v):
    """Coerce a config value into a list of non-empty strings. Accepts a JSON
    list (["admin", "root"]) or a comma-separated string ("admin,root")."""
    if isinstance(v, str):
        v = v.split(",")
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


def default_scan():
    """rtsp-scan range/ports/credentials from the bundled default_config.json,
    read fresh from disk so edits take effect between runs. Every field is a list
    of strings: `range` and `ports` accept a JSON list (["192.168.1.0/24",
    "192.168.2.0/24"], ["554", "8554"]) or a single/comma-separated string;
    `ports` defaults to ["554"]. `logins`/`passwords` are the credential base the
    scan verifies (every login x every password) when the CLI gives no
    `--logins`/`--passwords`. This is what `rtsp-scan` uses given no options."""
    d = _load_defaults()
    s = d.get("scan") if isinstance(d.get("scan"), dict) else {}
    return {"range": _str_list(s.get("range")),
            "ports": _str_list(s.get("ports")) or ["554"],
            "logins": _str_list(s.get("logins")),
            "passwords": _str_list(s.get("passwords"))}
