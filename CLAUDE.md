# CLAUDE.md — Camera Viewer (ivms666)

Guidance for working in this repo. Read before editing.

> **HARD RULE — the *assistant* must never touch `cameraviewer/default_config.json`.**
> NEVER open, read, edit, or overwrite it with your tools. It is a
> **user-owned runtime input file** the user maintains by hand. The running
> *program* reads it at runtime (`config.default_scan` → `scan.range`,
> `scan.ports`, `scan.logins`, `scan.passwords` — each a JSON **list** of
> strings, e.g. `"range": ["192.168.1.0/24", "192.168.2.0/24"]`; a single or
> comma-separated string is still accepted); that is fine. Only the assistant is
> barred from reading/writing the file.

> **Keep this file current.** Whenever you discover something new about the
> device or codebase, hit a gotcha, or make an architectural decision, update
> the relevant section of this file **in the same change**. Especially record
> hard-won facts learned by probing the real DVR (formats, ranges, quirks,
> lockouts) — they are expensive to rediscover. Treat "Device gotchas" below as
> the running log.

## What this is

A **zero-dependency local GUI** to view snapshots / live view from a
Hikvision-style DVR/NVR and edit its motion-detection area. The "GUI" is a
local web app: a stdlib HTTP server serves a page and proxies camera requests;
the browser is the window. No `pip`, no `brew`, no framework — **Python
standard library only**. Keep it that way.

Run it:
```
python3 camera_viewer.py            # launches server + opens browser
python3 -m cameraviewer discover --user admin --password '...'   # CLI channel list
python3 -m cameraviewer rtsp-scan --range 192.168.1.0/24         # find RTSP ports + print links
python3 -m cameraviewer rtsp-scan                                # no args -> scan.range/ports from cameraviewer/default_config.json
python3 -m cameraviewer rtsp-scan --host <ip> --logins admin,root --passwords 12345,admin --output found.json
python3 -m cameraviewer import --file found.json                 # load scan output into the app (~/.camera_viewer.json)
```

> **rtsp-scan credential verification + output → import (the setup flow):**
> Credentials default to `scan.logins`/`scan.passwords` from `default_config.json`;
> passing `--logins`/`--passwords` (comma-separated) **overrides** that list
> entirely — when provided, only the provided values are used (each list
> overrides independently). For each RTSP port found, the scan tries **every
> login with every password**, in order (login 1 × all passwords, then login 2,
> …), **sequentially** (concurrent auth can trip a DVR failed-login lockout) and
> **stopping at the first accepted credential per port** (a port needs only one
> working login; `probe_credentials(stop_on_first=True)`), answering the device's
> Digest/Basic 401 with an authenticated `DESCRIBE`; a final `200` means the
> credential works. **Hosts are scanned in parallel** — `scan_and_verify` runs a
> pool of `min(--parallel, #hosts)` threads (default `--parallel=10`), one host
> per thread, doing detect+verify end-to-end; **credentials stay sequential
> *within* a host** (never concurrent auth to one DVR). Progress is therefore
> per-host on one line (`scanning N of M hosts`), not per-credential. Verified
> hits are written to `--output`
> (default `rtsp-scan-output.json`) as an import-ready `{"devices":[…]}` file,
> which `import --file <path>` merges into the internal config
> (`~/.camera_viewer.json`, deduped by host+rtsp_port+user). The device's
> **HTTP/ISAPI `port`** (snapshots/motion) can't be discovered by an RTSP scan,
> so the output defaults it to `80` — adjust after import if the web port differs.

> **rtsp-scan safety:** only scan IPs/ranges you own. `--range` accepts CIDR,
> dash (`10.0.0.5-9` last-octet shorthand, or full `10.0.0.5-10.0.1.20` which
> rolls over octet boundaries), or a single IP. A wide public range (e.g. a `/16`
> of someone's ISP block) is mass-scanning third-party hosts — don't.
> **CIDR starts at the exact address written** and goes UP to the top of the block
> (never below): `192.0.0.62/24` -> `.62 … .255`; a host part of 0
> (`192.0.0.0/24`) covers the whole block. Network/broadcast addresses are
> included (`expand_range` uses `ip_interface`, not `.hosts()`).

## The target device (known facts)

- Target: a real Hikvision OEM DVR. **Its IP, ports, and credentials are NOT in
  this repo** — they live only in `~/.camera_viewer.json` (0600). Never hardcode
  them into source or docs. Treat writes to it carefully.
- Identity: `Server: DNVRS-Webs`, Digest realm `DVRNVRDVS` → **Hikvision OEM DVR**, ~2015 firmware.
- Auth: **HTTP Digest** (Basic also offered). `urllib` handles both via
  `HTTPPasswordMgrWithDefaultRealm` + digest/basic handlers.
- 4 analog cameras. Channel model:
  - **Picture/stream id** = `<n>01` (101, 201, 301, 401) → `/ISAPI/Streaming/channels/<id>/picture`.
  - **Video input index** = `n` (1..4) → `/ISAPI/System/Video/inputs/channels/<n>/motionDetection`.
  - `discover_channels` returns both: `{"id": "101", "input": "1", "name": ...}`.
- **Live video / ports (hard-won):** over the DVR's **HTTP port** there is **no
  real-time video** — RTSP-over-HTTP → 404, MJPEG `httpPreview` → 403, and the
  channel advertises exactly one live transport:
  `<streamingTransport opt="RTSP">RTSP</streamingTransport>`. So real-time needs
  **RTSP (internal port 554)** forwarded to an external port; that external port
  is the per-camera **`rtsp_port`** setting, and `live.py` builds
  `rtsp://user:pass@host:<rtsp_port>/Streaming/Channels/<id>` (sub-stream = id
  ending in 02). **iVMS-4500 does NOT use RTSP/HTTP** — it speaks Hikvision's
  proprietary **SDK/service protocol** (internal port 8000). A forwarded SDK port
  (TCP opens but ignores HTTP *and* RTSP, waits for a binary handshake) is useless
  to us — browsers/ffmpeg can't decode it and we won't reimplement the NET_DVR
  SDK. Identify a real RTSP port by an `OPTIONS` reply of `RTSP/1.0 200 OK` (or
  `401 Unauthorized` with `realm="Embedded Net DVR"`). A real external RTSP port
  was verified end-to-end (HD ffmpeg mpjpeg transcode, ~15 fps). Live view is
  **locked to the main stream (HD)** — no sub-stream toggle in the UI
  (`stream=main`); `live.py` retains sub support for potential reuse. The add-form
  RTSP-port default is the standard **554** (users set their own forwarded port).
- **Snapshot quality quirk:** the picture endpoint defaults to **704×576 (D1)**
  even though the main stream is 1080p. Requesting
  `?videoResolutionWidth=1280&videoResolutionHeight=720` returns real 720p;
  **1920×1080 is rejected** by this DVR. So 720p is the best still it gives —
  `fetch_snapshot` requests a resolution and silently falls back on rejection.
- **Motion format:** grid `gridMap`, **22 cols × 18 rows**, MSB-first, and
  **row-aligned** — each row is padded to whole bytes: `ceil(22/8)=3` bytes/row
  × 18 rows = 54 bytes = **108 hex chars** (22 real cells + 2 padding bits per
  row). It is *not* a flat 396-bit stream (that was an early bug). Padding bits
  are often set to 1 by the device (a fully-on grid reads as `"f"*108`).
  → `motion.decode_gridmap` / `apply_cells` handle this; `apply_cells` edits the
  current map bit-by-bit so **padding and any unknown bits are preserved**.
- **Motion sensitivity:** `sensitivityLevel` is a small **0–6** scale (NOT
  0–100). The capabilities endpoint does not expose the range — it was found by
  probing. Real values seen: 2–3. Writing an out-of-range value (e.g. `7`)
  returns **HTTP 403 and then locks all config writes** on the device for a
  while (see gotchas). `motion.set_motion` clamps to `[SENS_MIN, SENS_MAX]` =
  `[0, 6]`; the UI slider is 0–6. **Never send an unclamped value.**

## Motion detection: AREA editor vs LIVE indicator (two separate things)

- **Area editor** (`motion.py`, the per-tile ⚙ → "Motion detection area"): edits
  *where* the DVR looks (the gridMap) and sensitivity. Read-modify-write, guarded.
- **Live indicator** (`events.py`, `/motion/state`): shows *when* the DVR fires
  motion, per camera. The server holds one long-lived GET per device on
  `/ISAPI/Event/notification/alertStream` (a `multipart/mixed` push of
  `<EventNotificationAlert>` XML). We act on `eventType=VMD`: a channel is
  "active" if a VMD `active` event arrived within `HOLD_SECONDS` (≈6s) — the hold
  window covers firmwares that repeat `active` and then stop without sending
  `inactive`. `channelID` in the event is the **video-input index** (1..4) = the
  tile's `input`; the picture id for auto-save is `<input>01`.
- On a fresh inactive→active transition the monitor **auto-saves** a max-res JPEG
  (debounced ≤1/channel/10s) and the browser shows a red MOTION badge/glow +
  a full-screen popup of the captured frame. Monitors **start at server startup**
  (`events.start_all()`), so capture works even with no browser open.
- A `404/501/403` on the alert stream marks the device "unsupported" and the
  monitor **stops** (don't hammer — a repeated 403 risks the write-lockout).
- **Saving** is server-side (`store.save_snapshot`) to the configured folder
  (header ⚙ Settings → `save_path`, default `~/CameraViewer`), always at
  `camera.MAX_STILL_RES` (720p — this DVR's max still). Manual **Save** and motion
  auto-capture use the same path. The browser can't write arbitrary paths, so the
  path is a server setting, not a browser download.
- **Frontend note:** per-tile actions are `Live | Save | ⚙`; the ⚙ menu holds
  "Motion detection area" and "Hide this camera" (the old ✕).

## Architecture / structure to follow

```
camera_viewer.py            # thin launcher -> cameraviewer.cli.main()
cameraviewer/
  config.py                 # device store + JSON persistence (~/.camera_viewer.json, 0600); app settings (save_path); device-file import/export
  camera.py                 # ISAPI HTTP (get/put/open_stream), snapshot, MAX_STILL_RES, channel discovery, safe XML parse
  motion.py                 # gridMap codec + get_motion / set_motion (read-modify-write)  [detection AREA editor]
  events.py                 # motion ALERT stream: per-device daemon watches /ISAPI/Event/notification/alertStream, tracks per-channel VMD state (hold window), auto-saves on motion. get_state / start_all / ensure / stop
  store.py                  # server-side snapshot saving to config save_path at MAX_STILL_RES (manual Save + motion auto-capture)
  diagnose.py               # per-device health check of the motion->email->UI pipeline (motion enabled/area, VMD email+center linkage, SMTP) + safe auto-fix (RMW adds missing email/center to VMD triggers)
  live.py                   # RTSP->MJPEG via ffmpeg (real-time Live view); rtsp_url + check + open_mjpeg
  scan.py                   # rtsp-scan: probe ports via RTSP OPTIONS; verify creds via authed DESCRIBE; scan_and_verify (host-parallel pipeline) + expand_range/expand_ranges + rtsp_link + device_entry
  default_config.json       # USER-OWNED runtime input (devices=[], scan range/ports). Assistant: never read/edit. config.py reads it at runtime only.
  web.py                    # loads static/index.html + static/app.js
  server.py                 # BaseHTTPRequestHandler routes + ThreadingHTTPServer + run_gui()
  cli.py                    # argparse: GUI (default), `discover`, `rtsp-scan`, `import`
  __main__.py               # `python3 -m cameraviewer`
  static/index.html         # markup + CSS (frontend)
  static/app.js             # all frontend JS
tests/                      # unittest; helpers.py has the fake camera transport + FakeProc
```

Layering rule: `config` and `camera` are leaves. `motion` depends on `camera`.
`store` depends on `camera`+`config`. `events` depends on `camera`+`config`+`store`.
`server` depends on `config`/`camera`/`motion`/`events`/`store`/`live`/`web`.
`cli` depends on `server`/`camera`. Don't create cycles.

**Cross-module calls go through the module, not the name** — e.g. `motion.py`
calls `camera.camera_get(...)` (via `from . import camera`), never
`from .camera import camera_get`. This is deliberate: tests fake the network by
patching `camera.camera_get` / `camera_put`, and that only works if callers look
the name up on the module at call time. Preserve this pattern.

### HTTP API (browser ↔ server)

| Method | Path | Purpose |
|---|---|---|
| GET | `/`, `/app.js` | frontend |
| GET | `/devices` | list devices (**masked — no password**) |
| POST | `/devices` | add device |
| PUT | `/devices/<id>` | update (name/host/port/user/password/hidden) |
| DELETE | `/devices/<id>` | remove device |
| GET | `/channels?device=<id>` | discover cameras |
| GET | `/snapshot?device=<id>&ch=<cid>&res=<WxH>` | one JPEG |
| GET | `/motion?device=<id>&input=<n>` | current motion config (detection area) |
| POST | `/motion` `{device,input,cells,sensitivity}` | save motion area |
| GET | `/motion/state?device=<id>` | live per-channel motion state `{ok,supported,message,channels:{input:bool}}` (from the alert stream) |
| GET | `/settings` | app settings `{save_path}` |
| PUT | `/settings` `{save_path}` | update settings (path `~`-expanded, made absolute) |
| POST | `/save` `{device,ch}` | save a max-res (720p) JPEG to `save_path`, returns `{ok,path}` |
| POST | `/reboot` `{device}` | reboot the whole device (`PUT /ISAPI/System/reboot`); per-device, drops all streams ~1 min |
| GET | `/live/check?device=<id>` | pre-flight: ffmpeg present + RTSP port reachable |
| GET | `/live?device=<id>&ch=<cid>&stream=main\|sub` | live MJPEG stream (RTSP→ffmpeg), runs until the browser disconnects |

## Non-negotiable invariants

1. **Stdlib only for Python deps.** No third-party *Python packages*, ever. The
   one allowed **external binary** is **ffmpeg**, used solely by `live.py` for the
   real-time Live view (RTSP→MJPEG) while a Live window is open. The dashboard
   gallery stays on stdlib JPEG snapshots. Don't add other binaries or any pip
   package without discussing.
2. **Passwords never reach the browser.** `config.mask()` strips them; the
   browser proxies snapshots through the server. Never add an endpoint that
   returns a stored password. Config file is chmod `0600`, lives in `$HOME`
   (not the repo) so secrets aren't committed.
3. **Motion writes are read-modify-write and minimal.** `set_motion`:
   fresh GET → build the new `<gridMap>` with **`apply_cells`** (flips only the
   painted cells' bits in the *current* map; preserves padding and any bit we
   don't understand; same length) → optionally clamp+replace the layout
   `<sensitivityLevel>` → PUT. Everything else (e-mail/notification linkage lives
   under `/ISAPI/Event/triggers`, not here) stays byte-for-byte intact.
   **Guarded by a self-check** (`apply_cells(cur, decode(cur)) == cur`) that
   refuses to write on odd-length/invalid hex. **Always clamp sensitivity to
   0–6** before sending. Never bypass either guard.
4. **XML is parsed via `camera.parse_xml`**, which rejects DOCTYPE/ENTITY
   (XXE / billion-laughs defense without a dependency). Don't call
   `ET.fromstring` directly on camera responses.
5. **Frontend stays in `static/`.** Don't inline HTML/JS back into Python.
   All user-visible interpolation in `app.js` goes through `escapeHtml`.

## Testing

```
python3 -m unittest discover -s tests -t .
```

- No network: tests patch the camera transport via `tests/helpers.FakeCamera`
  (a context manager swapping `camera.camera_get`/`camera_put`). The **real**
  discovery/motion/server logic runs on top of the fake.
- Coverage lives in `test_motion` (codec + RMW + self-check refusal),
  `test_camera` (discovery tiers + resolution/fallback + XML safety),
  `test_config` (CRUD, masking, 0600 persistence, reload), `test_server`
  (end-to-end HTTP, password-not-leaked, all routes).
- When adding a feature, add a test at the lowest layer it belongs to, and an
  end-to-end `test_server` case if it adds/changes a route.
- Manual device check when needed: `discover` subcommand, or the codec sanity
  checks. Be cautious running `set_motion` against the real DVR — it writes.

## Device gotchas / hard-won facts (running log — append as you learn)

- **`alertStream` only carries an event if its linkage has `center` ("Notify
  Surveillance Center").** The live-motion indicator (`events.py`) reads
  `/ISAPI/Event/notification/alertStream`; the DVR pushes a `VMD` event there
  **only when the VMD trigger's `EventTriggerNotificationList` includes
  `<notificationMethod>center</notificationMethod>`**. `record`/`email` linkages
  fire independently but do **not** put the event on the stream. Symptom: motion
  detection is enabled with a painted area, yet the stream shows only
  `videoloss/inactive` keepalives and the app never badges. Fix = add a `center`
  notification to `/ISAPI/Event/triggers/VMD-<n>` (read-modify-write, preserving
  the existing `record`/`email` entries). Verified on the `home` DVR: after
  adding `center`, `VMD/active` events appeared immediately. So **the app's motion
  feature requires `center` to be enabled on each channel's VMD trigger** — this
  is a device-side prerequisite, not something the app can synthesize.
- **`/ISAPI/Event/triggers/VMD-<n>`** is the per-channel motion trigger
  (`videoInputChannelID` = input 1..4); `/ISAPI/Event/triggers` lists them all.
  Namespace here is `http://www.std-cgi.com/...` (not `hikvision.com`). Email SMTP
  lives at `/ISAPI/System/Network/mailing`. An absent/disabled input returns 403
  on its `motionDetection` (seen on `home` input 4).
- **Out-of-range config write → 403 + write lockout.** Writing
  `sensitivityLevel=7` (max is 6) returned HTTP 403 *and* left the device
  rejecting **all** subsequent config PUTs with 403 for a while (GET still
  works). This is a Hikvision "illegal operation" IP lockout; it clears after
  ~minutes–30 min or a DVR reboot. **Do not hammer** — retrying extends it.
  Lesson: only ever write **in-range, clamped** values; the app clamps, so this
  only bites manual probing. (Incident: a range-probe left Camera 01 sensitivity
  at 7 instead of 3 and locked writes; it needed to be reset once unlocked.)
- **Snapshot resolution:** stills default to 704×576; `?videoResolutionWidth=1280
  &videoResolutionHeight=720` gives real 720p; **1080p is rejected**. So 720p is
  the max still.
- **gridMap length is 108, row-aligned** (see device facts above) — an early
  flat-396-bit codec could not reproduce it and the self-check (correctly)
  refused to save.
- **Probing the real DVR writes state.** Prefer read-only GETs. If you must
  write to learn something, use in-range values and restore the original XML in
  a `finally` — and expect a possible lockout anyway.
- **No `min`/`max` in capabilities.** `/ISAPI/.../motionDetection/capabilities`
  just echoes current values (no range attributes), so ranges must be learned
  empirically and recorded here.

## Conventions

- 4-space indent, snake_case, module-level docstrings, keep functions small.
- Broad `except Exception` is used intentionally at the camera boundary to turn
  device failures into UI messages (`Handler._explain`) — keep those localized,
  don't swallow errors in pure logic.
- Comments explain *why* (esp. the gridMap bit-order and the self-check), not *what*.
