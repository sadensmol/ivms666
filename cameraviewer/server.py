"""HTTP request handler and server.

Routes:
    GET  /                     the web GUI
    GET  /app.js               frontend script
    GET  /devices              list devices (masked — no passwords)
    POST /devices              add a device
    PUT  /devices/<id>         update a device (name/host/port/user/password/hidden)
    DELETE /devices/<id>       remove a device
    GET  /channels?device=<id> discover the device's cameras
    GET  /snapshot?device=<id>&ch=<cid>&res=<WxH>   a JPEG still
    GET  /motion?device=<id>&input=<n>              current motion config
    POST /motion  {device, input, cells, sensitivity}   save motion area
    GET  /motion/state?device=<id>                  live motion state per channel
    GET  /settings                                  app settings (image save path)
    PUT  /settings  {save_path}                     update app settings
    POST /save  {device, ch}                        save a max-res JPEG to disk
    POST /reboot  {device}                          reboot the whole device (ISAPI)
    GET  /diagnose?device=<id>                       audit motion->email->UI pipeline
    POST /diagnose/fix  {device}                     auto-fix the fixable linkage gaps
"""

import http.server
import json
import socketserver
import threading
import urllib.error
import webbrowser
from urllib.parse import parse_qs, urlparse

from . import camera, config, diagnose, events, live, motion, store, web

_LIVE_FIRST_READ = 512   # bytes to wait for before declaring the stream alive
_LIVE_CHUNK = 8192


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep the console quiet / avoid leaking request details

    # --- low-level helpers -------------------------------------------------
    def _send(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    def _query(self):
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def _cfg_from_query(self):
        return config.get_cfg(self._query().get("device", ""))

    # --- GET ---------------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, web.PAGE, "text/html; charset=utf-8")
            return
        if path == "/app.js":
            self._send(200, web.APP_JS, "application/javascript; charset=utf-8")
            return
        if path == "/devices":
            self._json(200, config.list_devices())
            return
        if path == "/settings":
            self._json(200, config.get_settings())
            return
        if path == "/motion/state":
            device_id = self._query().get("device", "")
            if not config.get_cfg(device_id):
                self._send(404, "unknown device")
                return
            self._json(200, events.get_state(device_id))
            return
        if path == "/diagnose":
            device_id = self._query().get("device", "")
            cfg = config.get_cfg(device_id)
            if not cfg:
                self._send(404, "unknown device")
                return
            try:
                self._json(200, diagnose.diagnose(cfg, config.get_hidden(device_id)))
            except Exception as e:  # noqa: BLE001
                self._json(200, {"error": self._explain(e)})
            return
        if path in ("/live", "/live/check"):
            cfg = self._cfg_from_query()
            if not cfg:
                self._send(404, "unknown device")
                return
            if path == "/live/check":
                ok, message = live.check(cfg)
                self._json(200, {"ok": ok, "message": message})
            else:
                self._handle_live(cfg)
            return
        if path in ("/channels", "/snapshot", "/motion"):
            cfg = self._cfg_from_query()
            if not cfg:
                self._send(404, "unknown device")
                return
            self._handle_camera_get(path, cfg)
            return
        self._send(404, "not found")

    def _handle_live(self, cfg):
        """Stream RTSP->MJPEG (via ffmpeg) to the browser until it disconnects."""
        if not live.ffmpeg_available():
            self._send(503, "ffmpeg is not installed on the server (e.g. `brew install ffmpeg`)")
            return
        q = self._query()
        url = live.rtsp_url(cfg, q.get("ch", "101"), q.get("stream", "main"))
        proc = live.open_mjpeg(url)

        first = proc.stdout.read(_LIVE_FIRST_READ)
        if not first:  # ffmpeg produced nothing -> connection/auth/path failure
            err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()[:300]
            live.terminate(proc)
            self._send(502, "live stream failed: " + (err or "no data (is the RTSP port open?)"))
            return

        # Drain stderr in the background so its pipe never blocks ffmpeg mid-stream.
        threading.Thread(target=lambda: proc.stderr.read(), daemon=True).start()
        self.send_response(200)
        self.send_header("Content-Type", live.MJPEG_CONTENT_TYPE)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(first)
            while True:
                chunk = proc.stdout.read(_LIVE_CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser closed the Live window
        finally:
            live.terminate(proc)

    def _handle_camera_get(self, path, cfg):
        try:
            if path == "/channels":
                self._json(200, camera.discover_channels(cfg))
            elif path == "/snapshot":
                q = self._query()
                ctype, data = camera.fetch_snapshot(cfg, q.get("ch", "101"), q.get("res"))
                self._send(200, data, ctype, {"Cache-Control": "no-store"})
            else:  # /motion
                self._json(200, motion.get_motion(cfg, self._query().get("input", "1")))
        except Exception as e:  # noqa: BLE001 - surface any camera failure to the UI
            self._send(502, self._explain(e))

    # --- POST --------------------------------------------------------------
    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/devices":
            try:
                p = self._read_json()
            except json.JSONDecodeError:
                self._send(400, "bad json")
                return
            if not p.get("host") or not p.get("port"):
                self._send(400, "host and port are required")
                return
            masked = config.add_device(p)
            events.ensure(masked["id"])  # start watching the new device for motion
            self._json(200, masked)
            return
        if path == "/save":
            try:
                p = self._read_json()
            except json.JSONDecodeError:
                self._json(200, {"ok": False, "message": "bad json"})
                return
            cfg = config.get_cfg(str(p.get("device", "")))
            if not cfg:
                self._json(200, {"ok": False, "message": "unknown device"})
                return
            try:
                saved = store.save_snapshot(cfg, str(p.get("ch", "101")),
                                            label=config.device_label(str(p.get("device", ""))))
                self._json(200, {"ok": True, "path": saved})
            except Exception as e:  # noqa: BLE001
                self._json(200, {"ok": False, "message": self._explain(e)})
            return
        if path == "/diagnose/fix":
            try:
                p = self._read_json()
            except json.JSONDecodeError:
                self._json(200, {"ok": False, "message": "bad json"})
                return
            cfg = config.get_cfg(str(p.get("device", "")))
            if not cfg:
                self._json(200, {"ok": False, "message": "unknown device"})
                return
            try:
                result = diagnose.apply_fixes(cfg)
                self._json(200, {"ok": True, **result})
            except Exception as e:  # noqa: BLE001
                self._json(200, {"ok": False, "message": self._explain(e)})
            return
        if path == "/reboot":
            try:
                p = self._read_json()
            except json.JSONDecodeError:
                self._json(200, {"ok": False, "message": "bad json"})
                return
            cfg = config.get_cfg(str(p.get("device", "")))
            if not cfg:
                self._json(200, {"ok": False, "message": "unknown device"})
                return
            try:
                ok, message = camera.reboot(cfg)
                self._json(200, {"ok": ok, "message": message})
            except Exception as e:  # noqa: BLE001
                self._json(200, {"ok": False, "message": self._explain(e)})
            return
        if path == "/motion":
            try:
                p = self._read_json()
            except json.JSONDecodeError:
                self._json(200, {"ok": False, "message": "bad json"})
                return
            cfg = config.get_cfg(str(p.get("device", "")))
            if not cfg:
                self._json(200, {"ok": False, "message": "unknown device"})
                return
            try:
                ok, message = motion.set_motion(cfg, str(p["input"]),
                                                p["cells"], p.get("sensitivity"))
                self._json(200, {"ok": ok, "message": message})
            except Exception as e:  # noqa: BLE001
                self._json(200, {"ok": False, "message": self._explain(e)})
            return
        self._send(404, "not found")

    # --- PUT ---------------------------------------------------------------
    def do_PUT(self):
        path = urlparse(self.path).path
        if path == "/settings":
            try:
                p = self._read_json()
            except json.JSONDecodeError:
                self._send(400, "bad json")
                return
            self._json(200, config.update_settings(p))
            return
        parts = path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "devices":
            try:
                p = self._read_json()
            except json.JSONDecodeError:
                self._send(400, "bad json")
                return
            masked = config.update_device(parts[1], p)
            if masked is None:
                self._send(404, "unknown device")
                return
            self._json(200, masked)
            return
        self._send(404, "not found")

    # --- DELETE ------------------------------------------------------------
    def do_DELETE(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "devices":
            config.delete_device(parts[1])
            events.stop(parts[1])  # retire its motion monitor
            self._json(200, {"ok": True})
            return
        self._send(404, "not found")

    @staticmethod
    def _explain(e):
        if isinstance(e, urllib.error.HTTPError):
            if e.code == 401:
                return "auth failed (check username/password)"
            if e.code == 403:
                return ("device refused the write (403) — value out of range or "
                        "config temporarily locked; wait a few minutes and retry")
            return f"HTTP {e.code}: {e.reason}"
        if isinstance(e, urllib.error.URLError):
            return f"cannot reach camera: {e.reason}"
        return f"{type(e).__name__}: {e}"


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def run_gui():
    config.load()
    events.start_all()  # begin watching every device for motion (works headless)
    url = f"http://{config.LISTEN_HOST}:{config.LISTEN_PORT}/"
    server = Server((config.LISTEN_HOST, config.LISTEN_PORT), Handler)
    print(f"Camera Viewer running at {url}")
    print(f"Devices stored in {config.CONFIG_PATH}")
    print("Press Ctrl+C to stop.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()
