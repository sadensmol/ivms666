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

# {"devices": [{id, name, host, port, user, password, hidden:[ids]}]}
_state = {"devices": []}
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
