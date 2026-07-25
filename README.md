# Camera Viewer

A tiny **zero-dependency** (Python stdlib only — no `pip`/`brew`) local GUI for
Hikvision-style DVR/NVRs. Runs a small local web server and opens your browser.

## Run

```bash
python3 camera_viewer.py
```

Then in the browser: **+ Add camera** → enter host/port/username/password.
Credentials and your view setup are saved to `~/.camera_viewer.json` (chmod 600)
and restored on the next launch. Passwords stay on the server — the browser
never receives them.

List cameras from the terminal instead:

```bash
python3 -m cameraviewer discover --host <camera-ip> --port 80 \
    --user admin --password 'secret'
```

## Features

- **Multiple cameras/NVRs**, each with saved credentials.
- **Auto channel discovery** and a live snapshot gallery (one image per camera).
- **Quality** selector — HD 720p or SD (the DVR defaults stills to low-res).
- **Live** view — real-time video. The server transcodes the DVR's RTSP H.264 to
  a browser-playable MJPEG stream with **ffmpeg** (only external dependency, used
  only while a Live window is open). Requires: `ffmpeg` installed, and the DVR's
  **RTSP port (554) forwarded** on the router to an external port that you set as
  the camera's **RTSP port**. (iVMS works over one port because it uses
  Hikvision's proprietary SDK protocol, which browsers can't play — hence RTSP.)
- **Remove** any tile from the view (per-device, persisted); **Reset hidden** restores them.
- **Motion-detection area editor** — enlarge a camera, paint the detection grid,
  set sensitivity, and save it back to the device. Only the area/sensitivity are
  changed; e-mail and other linkage are untouched, and the write is gated by a
  round-trip self-check so it can't corrupt the grid.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

Tests run fully offline (the camera network layer and ffmpeg are faked).

See [CLAUDE.md](CLAUDE.md) for architecture, invariants, and device details.
