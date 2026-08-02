"""Real-time live view: transcode the DVR's RTSP H.264 into a browser-friendly
MJPEG stream using ffmpeg.

The DVR only exposes RTSP (port 554 by default, per-camera configurable) for
live video — a browser can't play RTSP/H.264 directly, so the server runs
ffmpeg to convert it to `multipart/x-mixed-replace` MJPEG, which an <img> renders
natively. ffmpeg is an external binary (the one non-stdlib dependency, used only
while a Live window is open).
"""

import atexit
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
from urllib.parse import quote

# ffmpeg's mpjpeg muxer frames each JPEG with a `--ffmpeg` boundary.
MJPEG_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=ffmpeg"
AUDIO_CONTENT_TYPE = "audio/mpeg"   # streamed MP3 for an <audio> player (audio-only streams)


def no_video(stderr):
    """True when ffmpeg found no video track to map (the stream is audio/metadata
    only — the DVR's SDP has no `m=video`). Such a still grab fails with 'Output
    file does not contain any stream'."""
    low = (stderr or "").lower()
    return "does not contain any stream" in low or "output file is empty" in low


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
    """The RTSP URL to stream. Credentials are URL-encoded so '#', '@', ':' in a
    password don't corrupt the URL.
      - legacy `rtsp_url` present -> used verbatim;
      - RTSP-only device (kind=rtsp) -> compose with its stored `path` (verbatim,
        NOT assumed to be /Streaming/Channels/<id>);
      - otherwise the Hikvision channel URL."""
    if cfg.get("rtsp_url"):
        return cfg["rtsp_url"]
    user = cfg.get("user") or ""
    pw = cfg.get("password") or ""
    cred = f"{quote(user, safe='')}:{quote(pw, safe='')}@" if user else ""
    base = f"rtsp://{cred}{cfg['host']}:{rtsp_port(cfg)}"
    if cfg.get("kind") == "rtsp":
        path = cfg.get("path") or "/"
        return base + (path if path.startswith("/") else "/" + path)
    cid = _sub_channel(channel_id) if stream == "sub" else str(channel_id)
    return f"{base}/Streaming/Channels/{cid}"


def _host_port(cfg):
    """(host, port) to probe for reachability — parsed from the stored URL for a
    URL-only device, else the ISAPI host + rtsp_port."""
    url = cfg.get("rtsp_url")
    if url:
        # (?:[^/]*@)? skips the whole userinfo (greedy to the LAST '@'), so a
        # password containing '@'/':' isn't mistaken for the host:port.
        m = re.search(r"://(?:[^/]*@)?([^:/]+)(?::(\d+))?", url)
        if m:
            return m.group(1), int(m.group(2) or 554)
        return "", 554
    return cfg["host"], int(rtsp_port(cfg))


def check(cfg):
    """Pre-flight for the Live view: is ffmpeg present and the RTSP port reachable?
    Returns (ok, message) so the UI can show a clear reason before streaming."""
    if not ffmpeg_available():
        return False, "ffmpeg is not installed on the server (e.g. `brew install ffmpeg`)"
    host, port = _host_port(cfg)
    s = socket.socket()
    s.settimeout(5)
    try:
        s.connect((host, port))
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, (f"cannot reach RTSP at {host}:{port} ({type(e).__name__}) — "
                       f"check the URL / forward TCP {port} on the router")
    finally:
        s.close()


def grab_still(url, width=None, timeout=15):
    """One JPEG frame from an RTSP URL (poster / Save for a URL-only RTSP device).
    Not cached — a live stream's frame changes. Returns (jpeg_bytes, stderr)."""
    vf = _SQUARE_PIXELS + (f",scale={int(width)}:-2" if width else "")
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-rtsp_transport", "tcp",
           "-timeout", RTSP_TIMEOUT_US, "-i", url, "-frames:v", "1", "-q:v", "4",
           "-vf", vf, "-f", "mjpeg", "-"]
    proc = spawn(cmd)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    finally:
        terminate(proc)   # reaped either way, and never left in the registry
    return out, (err or b"").decode("utf-8", "replace")


# RTSP socket I/O timeout (microseconds) so a dead/blocked stream — DESCRIBE
# answers but no media flows (RTP not forwarded through NAT, wrong path) — fails
# fast with "Operation timed out" instead of hanging. A working stream delivers a
# keyframe in a few seconds, well under this. (This ffmpeg build uses `-timeout`;
# older builds used `-stimeout` — not `-rw_timeout`, which errors here.)
RTSP_TIMEOUT_US = "10000000"  # 10s

# Force square-pixel output using the source's own metadata: multiply the coded
# width by the sample aspect ratio (SAR) and mark the result 1:1. A stream with
# non-square pixels (SAR != 1 — common on D1/analog and some RTSP cameras) would
# otherwise be displayed SQUISHED, since a JPEG/<img> has no SAR to correct it.
# When SAR is unknown ffmpeg treats it as 1, so square sources are unchanged.
_SQUARE_PIXELS = "scale='trunc(iw*sar/2)*2':ih,setsar=1"


def open_mjpeg(url, fps=15, quality=5):
    """Spawn ffmpeg to read RTSP (over TCP) and emit an MJPEG multipart stream on
    stdout. Returns the Popen. Caller must terminate() it when the client leaves."""
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-rtsp_transport", "tcp", "-timeout", RTSP_TIMEOUT_US,
        "-i", url,
        "-an",                       # no audio
        "-r", str(fps),
        "-q:v", str(quality),        # 2 (best) .. 31 (worst)
        "-vf", _SQUARE_PIXELS,       # correct aspect (SAR) so the stream isn't squished
        "-f", "mpjpeg", "-",
    ]
    return spawn(cmd)


def open_audio(url):
    """Spawn ffmpeg to read an RTSP **audio** track (over TCP) and emit a streamed
    MP3 on stdout, for a browser <audio> player. Used for audio-only streams (no
    `m=video`) — e.g. a DVR channel whose video encoder is off but audio flows.
    The DVR's audio is typically G.711 (pcm_mulaw); we re-encode to MP3 so every
    browser can play it. Returns the Popen; caller terminate()s it on disconnect."""
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-rtsp_transport", "tcp", "-timeout", RTSP_TIMEOUT_US,
        "-i", url,
        "-vn",                       # no video (there is none)
        "-c:a", "libmp3lame", "-b:a", "64k", "-ac", "1",
        "-flush_packets", "1",       # low-latency streaming
        "-f", "mp3", "-",
    ]
    return spawn(cmd)


# Every ffmpeg we spawn is registered here so it can be killed when the server
# exits. This is NOT bookkeeping for its own sake: the DVR serves ~ONE RTSP
# session, and an ffmpeg that outlives its parent keeps that session ESTABLISHED
# forever — after which every playback grab returns `453 Not Enough Bandwidth`
# and the whole event log shows broken thumbnails (see CLAUDE.md gotchas).
_procs = set()
_procs_lock = threading.Lock()


def spawn(cmd):
    """Popen an ffmpeg command with piped stdout/stderr and remember the child, so
    `terminate_all()` can kill it on shutdown. Use this instead of Popen directly."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with _procs_lock:
        _procs.add(proc)
    return proc


def terminate(proc):
    """Kill an ffmpeg process and reap it, ignoring the usual teardown errors."""
    with _procs_lock:
        _procs.discard(proc)
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


def terminate_all():
    """Kill every ffmpeg still running. Registered with atexit (and called from the
    server's SIGTERM path) so a stop/restart never strands an RTSP session."""
    with _procs_lock:
        procs = list(_procs)
    for proc in procs:
        terminate(proc)


atexit.register(terminate_all)


def _orphan_pids(ps_output, hosts):
    """PIDs of ORPHANED ffmpeg processes (parent already gone) streaming from one
    of `hosts` — i.e. children stranded by a previous run that was SIGKILLed, which
    atexit could not clean up. Input is `ps -Ao pid=,ppid=,command=` output. Matching
    is deliberately narrow (ffmpeg + rtsp:// + one of OUR device hosts) so we never
    kill an unrelated process."""
    pids = []
    for line in ps_output.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, cmd = parts
        if ppid != "1" or "ffmpeg" not in cmd or "rtsp://" not in cmd:
            continue
        if not any(h and h in cmd for h in hosts):
            continue
        try:
            pids.append(int(pid))
        except ValueError:
            pass
    return pids


def kill_orphans(hosts):
    """Reclaim the DVR's RTSP session at startup: kill ffmpeg children stranded by a
    previous run (see `_orphan_pids`). Returns the pids killed."""
    try:
        out = subprocess.run(["ps", "-Ao", "pid=,ppid=,command="],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001 - ps missing/blocked: nothing to reclaim
        return []
    killed = []
    for pid in _orphan_pids(out, hosts):
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except OSError:
            pass
    return killed
