"""Real-time live view: transcode the DVR's RTSP H.264 into a browser-friendly
MJPEG stream using ffmpeg.

The DVR only exposes RTSP (port 554 by default, per-camera configurable) for
live video — a browser can't play RTSP/H.264 directly, so the server runs
ffmpeg to convert it to `multipart/x-mixed-replace` MJPEG, which an <img> renders
natively. ffmpeg is an external binary (the one non-stdlib dependency, used only
while a Live window is open).
"""

import shutil
import socket
import subprocess
from urllib.parse import quote

# ffmpeg's mpjpeg muxer frames each JPEG with a `--ffmpeg` boundary.
MJPEG_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=ffmpeg"


def ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def rtsp_port(cfg):
    return str(cfg.get("rtsp_port") or "554")


def _sub_channel(channel_id):
    """Main stream id (e.g. 101) -> sub-stream id (102)."""
    try:
        base = (int(channel_id) // 100) * 100
        return str(base + 2)
    except ValueError:
        return channel_id


def rtsp_url(cfg, channel_id, stream="main"):
    """Build the Hikvision RTSP URL. Credentials are URL-encoded so passwords
    containing '#', '@', ':' etc. don't corrupt the URL."""
    cid = _sub_channel(channel_id) if stream == "sub" else str(channel_id)
    user = cfg.get("user") or ""
    pw = cfg.get("password") or ""
    cred = f"{quote(user, safe='')}:{quote(pw, safe='')}@" if user else ""
    return f"rtsp://{cred}{cfg['host']}:{rtsp_port(cfg)}/Streaming/Channels/{cid}"


def check(cfg):
    """Pre-flight for the Live view: is ffmpeg present and the RTSP port reachable?
    Returns (ok, message) so the UI can show a clear reason before streaming."""
    if not ffmpeg_available():
        return False, "ffmpeg is not installed on the server (e.g. `brew install ffmpeg`)"
    port = rtsp_port(cfg)
    s = socket.socket()
    s.settimeout(5)
    try:
        s.connect((cfg["host"], int(port)))
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, (f"cannot reach RTSP at {cfg['host']}:{port} ({type(e).__name__}) — "
                       f"forward TCP {port} on the router to the DVR")
    finally:
        s.close()


def open_mjpeg(url, fps=15, quality=5):
    """Spawn ffmpeg to read RTSP (over TCP) and emit an MJPEG multipart stream on
    stdout. Returns the Popen. Caller must terminate() it when the client leaves."""
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", url,
        "-an",                       # no audio
        "-r", str(fps),
        "-q:v", str(quality),        # 2 (best) .. 31 (worst)
        "-f", "mpjpeg", "-",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def terminate(proc):
    """Kill an ffmpeg process and reap it, ignoring the usual teardown errors."""
    try:
        proc.kill()
    except Exception:
        pass
    for pipe in (proc.stdout, proc.stderr):
        try:
            if pipe:
                pipe.close()
        except Exception:
            pass
    try:
        proc.wait(timeout=3)
    except Exception:
        pass
