"""Device store and persistence.

Devices (host/port/credentials + per-device hidden-tile setup) are persisted to
CONFIG_PATH as JSON, chmod 0600. All access goes through the thread-safe helpers
below; passwords never leave the server (see `mask`).
"""

import json
import os
import secrets
import threading

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8777
CONFIG_PATH = os.path.expanduser("~/.camera_viewer.json")
# Where snapshots (motion auto-captures + manual saves) are written by default.
DEFAULT_SAVE_PATH = os.path.expanduser("~/CameraViewer")
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
_state = {"devices": list(_DEFAULTS.get("devices") or [])}
_lock = threading.Lock()


def load():
    """Load persisted state from CONFIG_PATH (no-op if the file is absent)."""
    global _state
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


def device_cfg(d):
    """The subset a camera request needs (includes the password — server-side only)."""
    return {"host": d["host"], "port": str(d["port"]),
            "user": d.get("user", ""), "password": d.get("password", ""),
            "rtsp_port": str(d.get("rtsp_port") or "554")}


def mask(d):
    """Browser-safe device view — never leaks the stored password."""
    return {"id": d["id"], "name": d.get("name") or d["host"], "host": d["host"],
            "port": str(d["port"]), "user": d.get("user", ""),
            "rtspPort": str(d.get("rtsp_port") or "554"),
            "hasPassword": bool(d.get("password")), "hidden": d.get("hidden", [])}


# --- thread-safe public API -------------------------------------------------
def list_devices():
    with _lock:
        return [mask(d) for d in _state["devices"]]


def get_cfg(device_id):
    """Return a camera-config dict for the device, or None if unknown."""
    with _lock:
        d = _find(device_id)
        return device_cfg(d) if d else None


def add_device(fields):
    """Create a device from user-supplied fields; returns the masked view."""
    device = {
        "id": secrets.token_hex(4),
        "name": str(fields.get("name") or "").strip(),
        "host": str(fields["host"]).strip(),
        "port": str(fields["port"]).strip(),
        "user": str(fields.get("user") or ""),
        "password": str(fields.get("password") or ""),
        "rtsp_port": str(fields.get("rtsp_port") or "554"),
        "hidden": [],
    }
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
        for key in ("name", "host", "port", "user", "rtsp_port"):
            if key in fields and fields[key] is not None:
                d[key] = str(fields[key]) if key == "user" else str(fields[key]).strip()
        if fields.get("password"):
            d["password"] = str(fields["password"])
        if isinstance(fields.get("hidden"), list):
            d["hidden"] = [str(x) for x in fields["hidden"]]
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


def get_hidden(device_id):
    """The device's hidden-channel ids (tiles the user removed from view)."""
    with _lock:
        d = _find(device_id)
        return list(d.get("hidden", [])) if d else []


# --- app settings (image save path) ----------------------------------------
def get_settings():
    """App-wide settings. Currently just `save_path` (where snapshots land),
    falling back to DEFAULT_SAVE_PATH when unset."""
    with _lock:
        s = _state.get("settings") or {}
        return {"save_path": s.get("save_path") or DEFAULT_SAVE_PATH}


def update_settings(patch):
    """Apply partial settings updates and persist. `save_path` is expanded
    (`~`) and normalized to an absolute path. Returns the effective settings."""
    with _lock:
        s = dict(_state.get("settings") or {})
        if patch.get("save_path"):
            s["save_path"] = os.path.abspath(os.path.expanduser(str(patch["save_path"]).strip()))
        _state["settings"] = s
        _save_locked()
        return {"save_path": s.get("save_path") or DEFAULT_SAVE_PATH}


# --- device file import/export (rtsp-scan -> app setup) ---------------------
def write_devices_file(path, entries):
    """Write a list of device field dicts as import-ready {"devices":[...]} JSON.
    Used by `rtsp-scan --output` so a scan's verified hits can seed the app."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"devices": entries}, f, indent=2)


def read_devices_file(path):
    """Read a device list from an rtsp-scan output file. Accepts either the
    {"devices":[...]} shape or a bare top-level list."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    devs = data.get("devices") if isinstance(data, dict) else data
    return [d for d in devs if isinstance(d, dict)] if isinstance(devs, list) else []


def import_devices(entries):
    """Add devices from a scan output file into the store, skipping ones that
    duplicate an existing host+rtsp_port+user (so re-importing is idempotent) or
    lack a host. Returns (added, skipped)."""
    added = skipped = 0
    with _lock:
        def key(d):
            return (d.get("host"), str(d.get("rtsp_port") or "554"), str(d.get("user") or ""))
        seen = {key(d) for d in _state["devices"]}
        for e in entries:
            host = str(e.get("host") or "").strip()
            if not host or key(e) in seen:
                skipped += 1
                continue
            device = {
                "id": secrets.token_hex(4),
                "name": str(e.get("name") or "").strip(),
                "host": host,
                "port": str(e.get("port") or "80").strip(),
                "user": str(e.get("user") or ""),
                "password": str(e.get("password") or ""),
                "rtsp_port": str(e.get("rtsp_port") or "554"),
                "hidden": [],
            }
            _state["devices"].append(device)
            seen.add(key(device))
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
