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
    GET  /playback?device=<id>&ch=<track>&time=<YYYY-MM-DDTHH:MM:SS>  one recorded still (max-res)
    GET  /events?device=<id>&ch=<track>&hours=<n>   motion event log (from recordings)
    GET  /clip?device=<id>&ch=<track>&start=&end=   one motion clip as MP4 (RTSP->ffmpeg)
    GET  /watch?device=<id>&ch=&start=&end=         shared player page (no creds in the link)
    GET  /watch/info?device=<id>                    camera NAME only, for the shared page title

`&download=1` on /clip, /playback and /snapshot adds a Content-Disposition so the
browser saves the file (the clip is stream-copied, i.e. the recording's own quality).
"""

import http.server
import json
import os
import re
import signal
import socketserver
import sys
import threading
import urllib.error
import webbrowser
from datetime import datetime, timedelta
from urllib.parse import parse_qs, unquote, urlparse

import camera, config, diagnose, events, live, motion, playback, recordings, store, web

_LIVE_FIRST_READ = 512   # bytes to wait for before declaring the stream alive
_LIVE_CHUNK = 8192


def _safe_name(text, fallback="camera"):
    """A filename-safe slug — a Content-Disposition filename must not carry quotes,
    slashes or newlines (header injection / broken save dialogs)."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text or "")).strip("-")
    return slug[:60] or fallback


def _clip_filename(device_id, q):
    """`<device>-ch<track>-<start>.mp4` for a downloaded motion clip."""
    return (f"{_safe_name(config.device_label(device_id))}"
            f"-ch{_safe_name(q.get('ch', '101'))}"
            f"-{_safe_name(q.get('start', 'clip'))}.mp4")


def _still_filename(device_id, ch, when):
    """`<device>-ch<id>-<time>.jpg` for a downloaded frame (event frame or live)."""
    stamp = _safe_name(when, "") or datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{_safe_name(config.device_label(device_id))}-ch{_safe_name(ch)}-{stamp}.jpg"


def _rtsp_reason(err):
    """Turn an ffmpeg RTSP stderr blob into a short, actionable message."""
    e = (err or "").strip()
    low = e.lower()
    if "does not contain any stream" in low or "output file is empty" in low:
        return ("the stream has no video track — this URL serves only audio/metadata "
                "(the DVR's SDP has no m=video). Check the credentials or use a "
                "video-capable channel/path.")
    if "timed out" in low or "timeout" in low:
        return ("no video from the stream — the RTSP handshake worked but no frames "
                "arrived. Check the URL path/credentials, or that the camera's media "
                "port is reachable (RTP not blocked by NAT).")
    if "401" in e or "unauthorized" in low:
        return "authentication failed — check the username/password in the URL."
    if "404" in e or "not found" in low:
        return "stream path not found — check the URL path (e.g. /Streaming/Channels/101)."
    if "refused" in low or "unreachable" in low or "no route" in low:
        return "cannot connect — check the host/port and that it speaks RTSP."
    return e[:200] or "RTSP unreachable."


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

    # --- long-lived streaming responses (live MJPEG, audio, clip) ----------
    # A reverse proxy like cloudflared REJECTS an HTTP/1.0 close-delimited body
    # (no Content-Length AND no chunked framing) with a 502 Bad Gateway. That is
    # exactly what the old `send_response(200)` + raw `wfile.write` loop produced,
    # which is why Live/audio/clips worked on localhost but 502'd behind Cloudflare
    # while every other endpoint (bounded, Content-Length via `_send`) was fine.
    # Streaming with HTTP/1.1 chunked framing gives the proxy explicit boundaries
    # it can re-emit to the edge. Browsers handle chunked MJPEG/MP4 natively, so
    # the local path is unchanged.
    def _stream_start(self, ctype, extra=None):
        self.protocol_version = "HTTP/1.1"    # chunked transfer-encoding is undefined in HTTP/1.0
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")  # one-shot stream; close when it ends
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _stream_write(self, data):
        if data:
            self.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")

    def _stream_end(self):
        self.wfile.write(b"0\r\n\r\n")

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
            self._send(200, web.page(), "text/html; charset=utf-8", {"Cache-Control": "no-store"})
            return
        if path == "/app.js":
            self._send(200, web.app_js(), "application/javascript; charset=utf-8", {"Cache-Control": "no-store"})
            return
        if path == "/watch":   # shared event link — plays one clip, no credentials in the URL
            self._send(200, web.watch_page(), "text/html; charset=utf-8", {"Cache-Control": "no-store"})
            return
        if path == "/watch/info":   # what the shared page shows in its title — name only
            q = self._query()
            device_id = q.get("device", "")
            if not config.get_cfg(device_id):
                self._json(404, {"error": "unknown or removed camera"})
                return
            self._json(200, {"name": config.device_label(device_id), "ch": q.get("ch", "")})
            return
        if path == "/devices":
            self._json(200, config.list_devices())
            return
        if path == "/groups":
            self._json(200, config.list_groups())
            return
        if path == "/settings":
            self._json(200, config.get_settings())
            return
        if path == "/motion/state":
            device_id = self._query().get("device", "")
            cfg = config.get_cfg(device_id)
            if not cfg:
                self._send(404, "unknown device")
                return
            if cfg.get("kind") == "rtsp":   # URL-only stream: no ISAPI motion
                self._json(200, {"ok": False, "supported": False,
                                 "message": "motion detection needs a DVR", "channels": {}})
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
        if path == "/audio":
            cfg = self._cfg_from_query()
            if not cfg:
                self._send(404, "unknown device")
                return
            self._handle_audio(cfg)
            return
        if path == "/playback":
            cfg = self._cfg_from_query()
            if not cfg:
                self._send(404, "unknown device")
                return
            self._handle_playback(cfg)
            return
        if path == "/events":
            q = self._query()
            device_id = q.get("device", "")
            cfg = config.get_cfg(device_id)
            if not cfg:
                self._send(404, "unknown device")
                return
            hours = int(q.get("hours", "24") or "24")
            now = datetime.now()  # DVR shares the local wall clock; labeled Z (not real UTC)
            start = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
            end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                # thumbnails are grabbed live from the DVR recording (via /playback);
                # the event list itself is just times/durations.
                self._json(200, {"events": recordings.list_events(cfg, q.get("ch", "101"), start, end)})
            except Exception as e:  # noqa: BLE001
                self._json(200, {"error": self._explain(e)})
            return
        if path == "/clip":
            cfg = self._cfg_from_query()
            if not cfg:
                self._send(404, "unknown device")
                return
            self._handle_clip(cfg)
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
            err = (proc.stderr.read() or b"").decode("utf-8", "replace")
            live.terminate(proc)
            self._send(502, "live stream failed: " + _rtsp_reason(err))
            return

        # Drain stderr in the background so its pipe never blocks ffmpeg mid-stream.
        threading.Thread(target=lambda: proc.stderr.read(), daemon=True).start()
        self._stream_start(live.MJPEG_CONTENT_TYPE)
        try:
            self._stream_write(first)
            while True:
                chunk = proc.stdout.read(_LIVE_CHUNK)
                if not chunk:
                    break
                self._stream_write(chunk)
            self._stream_end()
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser closed the Live window
        finally:
            live.terminate(proc)

    def _handle_audio(self, cfg):
        """Stream an RTSP audio track -> MP3 (via ffmpeg) to a browser <audio>, for
        an audio-only stream (no video). Runs until the browser disconnects."""
        if not live.ffmpeg_available():
            self._send(503, "ffmpeg is not installed on the server (e.g. `brew install ffmpeg`)")
            return
        q = self._query()
        proc = live.open_audio(live.rtsp_url(cfg, q.get("ch", "rtsp")))
        first = proc.stdout.read(_LIVE_FIRST_READ)
        if not first:  # ffmpeg produced nothing -> connection/auth/no-audio failure
            err = (proc.stderr.read() or b"").decode("utf-8", "replace")
            live.terminate(proc)
            self._send(502, "audio stream failed: " + _rtsp_reason(err))
            return
        threading.Thread(target=lambda: proc.stderr.read(), daemon=True).start()
        self._stream_start(live.AUDIO_CONTENT_TYPE)
        try:
            self._stream_write(first)
            while True:
                chunk = proc.stdout.read(_LIVE_CHUNK)
                if not chunk:
                    break
                self._stream_write(chunk)
            self._stream_end()
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser closed the Audio window
        finally:
            live.terminate(proc)

    def _handle_clip(self, cfg):
        """Stream one recorded motion clip as MP4 (RTSP playback -> ffmpeg) to a
        browser <video>, running until the clip ends or the browser disconnects."""
        if not recordings.ffmpeg_available():
            self._send(503, "ffmpeg is not installed on the server (e.g. `brew install ffmpeg`)")
            return
        q = self._query()
        # Clip playback gets priority over event-log thumbnail grabs — the DVR only
        # serves ~one RTSP session, so this pauses grabs for the clip's duration.
        with playback.rtsp_priority():
            proc = recordings.clip_process(cfg, q.get("ch", "101"), q.get("start", ""), q.get("end", ""))
            first = proc.stdout.read(_LIVE_FIRST_READ)
            if not first:  # ffmpeg produced nothing -> no footage / RTSP unreachable
                err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()[:300]
                recordings.terminate(proc)
                self._send(502, "clip failed: " + (err or "no recording for that time, or RTSP port unreachable"))
                return
            threading.Thread(target=lambda: proc.stderr.read(), daemon=True).start()
            # `&download=1` -> the browser saves the file instead of playing it. The
            # clip is already the recording's own H.264 stream-copied (`-c:v copy`),
            # i.e. the DVR's highest resolution — there is no re-encode to lose.
            extra = {}
            if q.get("download"):
                extra["Content-Disposition"] = \
                    f'attachment; filename="{_clip_filename(q.get("device", ""), q)}"'
            self._stream_start("video/mp4", extra)
            try:
                self._stream_write(first)
                while True:
                    chunk = proc.stdout.read(_LIVE_CHUNK)
                    if not chunk:
                        break
                    self._stream_write(chunk)
                self._stream_end()
            except (BrokenPipeError, ConnectionResetError):
                pass  # browser closed the player
            finally:
                recordings.terminate(proc)

    def _handle_playback(self, cfg):
        """Grab one full-res still from the recording at the requested time."""
        if not live.ffmpeg_available():
            self._send(503, "ffmpeg is not installed on the server (e.g. `brew install ffmpeg`)")
            return
        q = self._query()
        try:
            start, end = playback.to_span(q.get("time", ""))
        except ValueError:
            self._send(400, "bad time (expected YYYY-MM-DDTHH:MM[:SS])")
            return
        url = playback.playback_url(cfg, q.get("ch", "101"), start, end)
        width = None
        if q.get("res"):  # e.g. "480x270" -> scale thumbnails down
            try:
                width = int(q["res"].lower().split("x")[0])
            except (ValueError, IndexError):
                pass
        data, err = playback.grab_frame(url, width=width)
        if not data.startswith(b"\xff\xd8"):  # not a JPEG -> playback failed
            msg = err.strip()[:250] or "no recording at that time, or RTSP port unreachable"
            self._send(502, "playback failed: " + msg)
            return
        # a recorded frame at a fixed past instant never changes -> let the browser
        # cache it so clicking the thumbnail (same URL) is instant and never re-grabs.
        headers = {"Cache-Control": "max-age=86400, immutable"}
        if q.get("download"):   # no `res` on the request -> the recording's own resolution
            headers["Content-Disposition"] = \
                f'attachment; filename="{_still_filename(q.get("device", ""), q.get("ch", "101"), q.get("time", ""))}"'
        self._send(200, data, "image/jpeg", headers)

    def _handle_camera_get(self, path, cfg):
        # A URL-only RTSP device has no ISAPI: one synthetic channel, and its still
        # is a single frame grabbed from the stream via ffmpeg (not an ISAPI picture).
        if cfg.get("kind") == "rtsp":
            self._handle_rtsp_get(path, cfg)
            return
        try:
            if path == "/channels":
                self._json(200, camera.discover_channels(cfg))
            elif path == "/snapshot":
                q = self._query()
                ctype, data = camera.fetch_snapshot(cfg, q.get("ch", "101"), q.get("res"))
                headers = {"Cache-Control": "no-store"}
                if q.get("download"):   # "⬇ Download frame" in the Live view
                    headers["Content-Disposition"] = \
                        f'attachment; filename="{_still_filename(q.get("device", ""), q.get("ch", "101"), "")}"'
                self._send(200, data, ctype, headers)
            else:  # /motion
                self._json(200, motion.get_motion(cfg, self._query().get("input", "1")))
        except Exception as e:  # noqa: BLE001 - surface any camera failure to the UI
            self._send(502, self._explain(e))

    def _handle_rtsp_get(self, path, cfg):
        """/channels + /snapshot for a URL-only RTSP device (no ISAPI)."""
        if path == "/channels":
            name = cfg.get("name") or (cfg.get("path") or "").lstrip("/") or "RTSP stream"
            self._json(200, [{"id": "rtsp", "input": "1", "name": name}])
            return
        if path == "/motion":
            self._send(400, "motion detection is not available for an RTSP-only stream")
            return
        if not live.ffmpeg_available():
            self._send(503, "ffmpeg is not installed on the server (e.g. `brew install ffmpeg`)")
            return
        q = self._query()
        width = None
        if q.get("res"):
            try:
                width = int(q["res"].lower().split("x")[0])
            except (ValueError, IndexError):
                pass
        data, err = live.grab_still(live.rtsp_url(cfg, "rtsp"), width=width)
        if not data.startswith(b"\xff\xd8"):
            if live.no_video(err):   # audio/metadata-only stream -> tell the UI to switch to audio mode
                self._send(200, json.dumps({"audio_only": True}),
                           "application/json", {"Cache-Control": "no-store"})
                return
            self._send(502, "stream grab failed: " + _rtsp_reason(err))
            return
        headers = {"Cache-Control": "no-store"}
        if q.get("download"):
            headers["Content-Disposition"] = \
                f'attachment; filename="{_still_filename(q.get("device", ""), "rtsp", "")}"'
        self._send(200, data, "image/jpeg", headers)

    # --- POST --------------------------------------------------------------
    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/devices":
            try:
                p = self._read_json()
            except json.JSONDecodeError:
                self._send(400, "bad json")
                return
            if not p.get("rtsp_url") and (not p.get("host") or not p.get("port")):
                self._send(400, "host and port are required (or an rtsp_url)")
                return
            masked = config.add_device(p)
            if masked.get("kind") != "rtsp":       # RTSP-only streams have no ISAPI alert stream
                events.ensure(masked["id"])         # start watching the new DVR for motion
            self._json(200, masked)
            return
        if path == "/groups":
            try:
                p = self._read_json()
            except json.JSONDecodeError:
                self._send(400, "bad json")
                return
            if not str(p.get("name") or "").strip():
                self._send(400, "group name is required")
                return
            self._json(200, config.create_group(p["name"]))
            return
        if path == "/save":
            try:
                p = self._read_json()
            except json.JSONDecodeError:
                self._json(200, {"ok": False, "message": "bad json"})
                return
            device_id = str(p.get("device", ""))
            cfg = config.get_cfg(device_id)
            if not cfg:
                self._json(200, {"ok": False, "message": "unknown device"})
                return
            ch = str(p.get("ch", "101"))
            label = config.device_label(device_id)
            try:
                if cfg.get("kind") == "rtsp":       # grab a frame from the stream via ffmpeg
                    data, err = live.grab_still(live.rtsp_url(cfg, ch), width=1280)
                    if not data.startswith(b"\xff\xd8"):
                        raise RuntimeError(_rtsp_reason(err))
                    saved = store.save_bytes(data, ch, label=label)
                else:
                    saved = store.save_snapshot(cfg, ch, label=label)
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
        if len(parts) == 2 and parts[0] == "groups":
            # ?devices=1 -> also delete every member device (cascade); else un-assign.
            cascade = self._query().get("devices") in ("1", "true", "yes")
            deleted = config.delete_group(unquote(parts[1]), delete_devices=cascade)
            for dev_id in deleted:
                events.stop(dev_id)  # retire each deleted device's motion monitor
            self._json(200, {"ok": True, "deleted": deleted})
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
    # A previous run that was killed (not stopped) can leave an ffmpeg behind, and
    # that child keeps the DVR's single RTSP session open — every playback grab then
    # answers 453 and the event log shows only broken thumbnails. Reclaim it first.
    stranded = live.kill_orphans(config.device_hosts())
    if stranded:
        print(f"Reclaimed {len(stranded)} stranded ffmpeg stream(s) from a previous run.")
    events.start_all()  # begin watching every device for motion (works headless)
    url = f"http://{config.LISTEN_HOST}:{config.LISTEN_PORT}/"
    server = Server((config.LISTEN_HOST, config.LISTEN_PORT), Handler)
    print(f"ivms666 running at {url}")
    print(f"Devices stored in {config.CONFIG_PATH}")
    print("Press Ctrl+C to stop.")
    # A detached container has no browser and no display; opening one there would
    # spawn junk processes for nobody. CV_NO_BROWSER is set in the image.
    if not os.environ.get("CV_NO_BROWSER"):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    # `docker stop` (and `kill`) send SIGTERM: exit through the normal path so
    # atexit runs live.terminate_all() and no ffmpeg keeps an RTSP session open.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        print("\nStopped.")
        server.shutdown()
        live.terminate_all()
