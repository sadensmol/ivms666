# CLAUDE.md — Camera Viewer (ivms666)

Guidance for working in this repo. Read before editing.

> **CORE PRINCIPLE — the DVR is the single source of truth.** All event data,
> thumbnails, clips, stills, and history come **live from the DVR** (ISAPI / RTSP)
> whenever shown. The app is **NOT** running 24/7, so it must **never** rely on
> locally-saved files (recorded videos, captured frames, JSON caches) as a *data
> source* — a local capture would be incomplete and misleading. The event log,
> its thumbnails, and playback are all fetched from the DVR on demand.
> (The only local writes are *user-initiated*: the manual **Save** button and
> `rtsp-scan --output`. There is no background "auto-capture on motion" — it was
> removed for exactly this reason.)

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
> **stopping at the first accepted credential per port** (`find_credential` — a
> login is "accepted" the moment the `DESCRIBE` challenge-answer is anything but a
> 401, i.e. 200 *or* 404). **Then `probe_streams` enumerates the port's actual
> cameras, vendor- and channel-aware:** the vendor is inferred from the 401 **realm**
> (`vendors.detect` — `Embedded Net DVR`→Hikvision, `Login to <32-hex hash>`→XiongMai
> (XM OEM), a bare serial / `Login to <serial>`→Dahua-family, …), and that vendor's
> stream-path schemas are tried first
> (then the others, then Generic). Each vendor (one module in `cameraviewer/vendors/`)
> groups **alternate syntaxes per stream** so a camera yields ONE url (canonical
> `/Streaming/Channels/101` wins over the `ISAPI`/`h264` aliases), and walks
> channels 1..`MAX_CHANNELS` (8), **stopping at the first channel with no stream**
> (so a 3-camera DVR reports 6 streams — main+sub of ch1-3 — and stops at the empty
> ch4). Every working stream becomes its own output entry. **Windowed, port-major
> (this is what the CLI runs):**
> `scan_and_verify` takes a **window of `min(--parallel, #hosts)` IPs**
> (default `--parallel=25`) and scans it **one port at a time** — probe port 1 on
> all IPs in the window at once, then port 2 on the **same** IPs, … up to the last
> configured port — then advances to the next window of IPs. (So it is NOT "every
> IP on port 554, then every IP on the next port"; it's the requested "these N IPs
> across all ports, then the next N IPs".) **ONE shared budget of `workers` slots
> covers BOTH activities** (`scan.scan_and_verify` — a single
> `BoundedSemaphore(win)`, `win = min(--parallel, #hosts)`): every unit of network
> work — probing an IP, OR verifying logins on a found port (`enumerate_streams`,
> slow: logins × vendor paths × channels) — holds **one** slot **while it runs**,
> so **at most `workers` run at once across both**. A probe (`_probe`) frees its
> slot the instant it finishes (in its `finally`); a found port then **acquires a
> fresh slot** for verification. So if K ports are login-probing, only `workers −
> K` slots are free for IP probing, and only when ALL `workers` are busy verifying
> does probing **block until a login-probe finishes** ("stuck on login probing",
> by design — that's the requested behaviour, and it's why the earlier "+68
> verifying" pile-up is gone: we never race ahead of verification). **A probe must
> NEVER keep its slot past the probe itself** — an earlier version handed the
> probe's slot straight to verification and released it only in a later *ordered
> collection* loop, so a few slow verifications holding slots **deadlocked the next
> port's acquire-loop** (symptom: "0/63934 hosts · 13 RTSP · 7 verifying" — 7 busy,
> 18 free, yet probing frozen, because the 18 "free" threads held uncollected
> slots the stuck acquire-loop could never release). A single slow/hanging device
> only ties up its own one slot; the rest keep working. `on_found` fires from the coordinator
> **in host order per port** (deterministic streaming) even though probing is
> concurrent. `on_host_done` fires as a window's probing finishes; `all_hits` and
> the `done` counter are lock-guarded, and callbacks fire **outside** that lock so
> the scan-lock and the CLI's output-lock never nest (no deadlock). Credentials
> for one host are tried one at a time (no per-host lockout); different hosts
> verify concurrently. (There is no longer a separate verify pool /
> `verify_workers`; the single budget replaced it.) Live
> callbacks drive a **multi-line live region** (`cli._LiveRegion`, ANSI cursor
> control — cursor-up + clear-to-end-of-screen, redrawn under a lock): permanent
> lines scroll ABOVE it (`on_found` → `✓ RTSP host:port`; `on_verified` prints an
> **accurate outcome from `enumerate_streams`'s `reason`** — `→ login OK …` with
> the stream links, or `→ login OK but no stream path matched` (creds fine, URL
> schema unknown — `reason=no_path`), or `→ connection dropped (after i/n on
> user:pass — deprioritise that login next run)` (`conn_dropped` — socket died
> mid-scan, NOT all combos tried; `enumerate_streams`/`find_credential_ex` return
> `last` = the combo it dropped on so the message names the offending login), or `→ no valid
> login (i/n combo(s) tried)` (`no_login` — every combo tried, each 401). The old
> message always printed `len(creds)` "combos tried" even when a login was found
> or the scan stopped early — misleading; now `attempts` is the real count), while the live block BELOW shows one line
> per host currently being verified — `host:port  trying user:pass (i/n)`, updated
> **in place**, one row per host — updated
> by `on_attempt(host, port, i, n, user, password)` threaded through to
> `find_credential` — plus a bottom `N/M hosts · X RTSP · Y verified · Z streams` line
> (`on_host_done`). **Two flood guards (`_LiveRegion._fit`) are non-negotiable:**
> each line is truncated to the terminal **width** (else it wraps onto a 2nd
> physical row) and the block is capped to the terminal **height** (surplus hosts
> collapse into one `… +N more verifying` line). Both matter because `render`'s
> `\033[nA` cursor-up **cannot climb above the top of the screen** — a block taller
> than the viewport (or a wrapped line) makes the move clamp, the scrolled-off rows
> are never cleared, and every redraw stacks a fresh copy below the stale one. That
> was the "tons of repeated `trying …` lines flooding the console" bug (with the
> default `--parallel 25`, up to ~26 live rows easily exceed a small/split pane).
> `_LiveRegion` is a **no-op on a non-tty** (piped/redirected),
> so captured output is just the clean sequence of permanent lines. Ctrl+C exits
> cleanly (caught in `cli.main`; in-flight probes finish on their socket timeout).
> Each verified hit is written to `--output` (default `rtsp-scan-output.json`)
> **the instant its credential is decoded** (in `on_verified`, not at the end of
> the run) via `config.merge_devices_file`, which **appends** and **never wipes**:
> it reads the existing file (recreating it if deleted), dedups, and rewrites — so
> a re-run adds to what's already there. Entries are the current config shape —
> `{"devices":[{"rtsp_url": "rtsp://user:pass@host:port<path>"}, …]}`
> (`scan.device_entry` → `rtsp_link`, one per enumerated stream; `<path>` is the
> vendor path that verified). `import --file <path>` (`config.import_devices`)
> **parses each `rtsp_url` into an RTSP-only device** (host/rtsp_port/user/password/
> path, `isapi_enabled=false`), deduped vs existing by rtsp_url so re-importing is
> idempotent; a legacy `{host,port,…}` entry still imports as a DVR.

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
- While a channel is in motion the browser shows a **blinking solid red border**
  around the tile (`.tile.motion .corners`, no text badge) and — **only if the
  `motion_popup` setting is on (default OFF)** — a full-screen popup (the shared
  `#imgOverlay` — one popup for both the motion snapshot and the event-frame
  lightbox, with a Close button + Esc + backdrop-click; `showImage()` for a static
  frame, `showMotionPopup()` for the live one) that **live-refreshes the 720p still
  every 1s** and **stays open until that channel's motion ends** (closed by
  `applyMotion` on the falling edge, not a timer). The toggle lives in ⚙ Settings
  ("Show full-screen popup when motion is detected"), persists in
  `config` settings, is loaded at startup into `motionPopupEnabled`, and saves the
  instant it's ticked; the red border always shows regardless. Suppressed while the
  Live view is open. A tile with **active
  motion also refreshes its snapshot every 1s** (`refreshMotionTiles`); all other
  tiles stay on the configured "Refresh (s)" interval. Monitors **start at server
  startup** (`events.start_all()`), so capture works even with no browser open.
- **Diagnose** (`diagnose.py`, per-device header button, `GET /diagnose` +
  `POST /diagnose/fix`). A channel is diagnosed in one of two modes:
  - **Live channel** (has a camera *and* not hidden): audits motion enabled/area +
    VMD `email`/`center` linkage + **recording** (motion-triggered mode + ≥10s
    pre/post-record) + **recording quality** — flag only when the main stream is
    **below HD (height < 720)**, NOT "below advertised max" (this DVR
    over-advertises 1080p on a 720p-only channel, so nagging to raise it would loop
    on a PUT the device rejects — see gotcha; 720p is HD and an acceptable ceiling).
    Auto-fixes: add missing `email`/`center` (RMW `/ISAPI/Event/triggers/VMD-<n>`),
    set the track to `MOTION` with 10s pre/post (RMW
    `/ISAPI/ContentMgmt/record/tracks/<id>`), and raise the main stream to the
    highest resolution the device **actually accepts** — `_set_stream_max_res` tries
    advertised standard pairs largest-first and keeps only one that PUTs without
    error AND **reads back changed** (deviceError/silent-revert → step down), so it
    never claims a false success.
  - **Unused channel** — either the input has **no camera**
    (`videoInputEnabled=false` / `resDesc=NO VIDEO`, read from
    `/ISAPI/System/Video/inputs/channels` via `_input_status`) **or** the user
    **hid** its tile (`config.get_hidden`). The full motion/quality checks are
    **skipped** (they'd 403 on an empty input and just nag about a camera you don't
    use). Instead it flags the one thing that wastes disk — **recording left ON** —
    and auto-fixes by disabling the record track (RMW `<Enable>false</Enable>` on
    `/ISAPI/ContentMgmt/record/tracks/<id>`; the track stays readable/writable even
    when the input itself is off). This is why the old quality fix "didn't work" on
    an empty slot: pushing 1080p to a NO-VIDEO channel 403s (and can lock it) — now
    such channels are never written that way.
  - **Device-wide:** SMTP configured, and the **DVR clock** vs real local time
    (`_clock_status`: GET `/ISAPI/System/time`, parse `<localTime>` incl. its
    `±HH:MM` offset, compare to `now(UTC)+offset`; flag if off by >120s). Fix
    `_set_clock` RMW-writes the correct local time into `<localTime>` keeping
    `timeMode` (already `manual`) + `<timeZone>`.
  - Non-fixable gaps (motion off, no area, SMTP unset) are reported with guidance.
  - **Gotcha:** the track PUT must NOT change `<DefaultRecordingMode>` (read-only →
    400 badXmlContent); only per-schedule `<ActionRecordingMode>` (CMR→MOTION),
    `<Pre/PostRecordTimeSeconds>`, and the top-level `<Enable>` are writable.
- A `404/501/403` on the alert stream marks the device "unsupported" and the
  monitor **stops** (don't hammer — a repeated 403 risks the write-lockout).
- **Saving** is server-side (`store.save_snapshot`) to the configured folder
  (header ⚙ Settings → `save_path`, default `~/CameraViewer`), always at
  `camera.MAX_STILL_RES` (720p — this DVR's max still). Manual **Save** and motion
  auto-capture use the same path. The browser can't write arbitrary paths, so the
  path is a server setting, not a browser download.
- **Event log** (`recordings.py`, per-tile ⚙ → "Event log"): once the DVR records
  on **motion** (diagnose fix), each recorded segment IS a motion event.
  `list_events` runs `POST /ISAPI/ContentMgmt/search` (`CMSearchDescription`, a
  well-formed GUID `searchID` or it 400s) on the channel's track over the last N
  hours and returns `{time, seconds, start, end}` per clip (newest first). The UI
  lists them; each row shows **ONE thumbnail grabbed live from the DVR recording** —
  a single lazy `<img>` at `/playback?...&time=&res=480x270`, sampled ~12s in (just
  after the 10s pre-record, where the motion is), via `playback.grab_frame`
  (width-scaled). **One, not three:** this DVR allows only ~one concurrent RTSP
  session (see the 453 gotcha), so 3 thumbs/event × several rows all 453'd — one
  reliable thumb beats three broken ones. **Never local files** (source of truth =
  DVR). Click a thumbnail → lightbox (`#imgOverlay`) showing the **same
  already-loaded image** (`img.src`, browser-cached `immutable`) — instant, no
  second grab, thumbnail-res (good enough; a fresh full-res grab would just 453).
  **▶ Play** streams the clip to an HTML5 `<video controls>` via `/clip` (RTSP
  playback → ffmpeg). **Clip playback takes RTSP priority** (the DVR serves ~one
  session): `_handle_clip` wraps the stream in `playback.rtsp_priority()`, which
  drains the in-flight grab then holds the grab semaphore for the clip's duration,
  so concurrent thumbnail grabs **stand down** (return "busy: playback in progress")
  instead of 453-ing the clip. The frontend also **pauses** unfinished thumbnails
  on ▶ Play (`pauseThumbs`, stashes `src`) and **resumes** them on close
  (`resumeThumbs`), so no thumbnail even requests the DVR while a clip plays.
  **ffmpeg gotcha:** the DVR's audio is **G.711/pcm_mulaw**,
  which can't be stream-copied into MP4 — `clip_process` uses `-an -c:v copy` (drop
  audio) with `frag_keyframe+empty_moov` so `<video>` plays as bytes arrive.
- **RTSP `453 Not Enough Bandwidth` — the DVR caps concurrent RTSP sessions to
  ~one.** Firing several playback grabs at once (the old 3-thumbs-per-row event log,
  or a full-res click-through while thumbs still load) makes every extra
  `DESCRIBE` return `RTSP/1.0 453 Not Enough Bandwidth`, so most thumbnails came
  back broken. Worse, a killed/timed-out grab can leave a session **lingering** on
  the DVR for ~tens of seconds, so even a lone grab 453s until it drains — that's
  why probing looked "permanently dead" mid-debug, then recovered after a pause.
  **Fixes in `playback.grab_frame`:** a module `threading.BoundedSemaphore(1)`
  serializes grabs (one RTSP session at a time), a 453 is **retried with backoff**
  (a slot frees when the previous grab's ffmpeg exits), and successful frames are
  held in a **bounded in-memory cache** (`_cache`, ~400 frames, keyed by
  (url,width)) so a click / re-open never re-grabs. `/playback` responses are sent
  `Cache-Control: max-age=86400, immutable` (a past frame never changes) so the
  browser caches them too. Verified: 6 concurrent grabs → 6/6 OK (serialized
  ~3.5s each) instead of all 453; 2nd identical grab = 0ms (cached). The in-memory
  cache is a **perf cache of live-fetched frames, not a data source** — bounded,
  never written to disk, dropped on restart — so it respects the source-of-truth rule.
- **A still from a past recording is EXPENSIVE on this DVR** — measured:
  ffmpeg-from-RTSP-playback ~4–9s (flaky, and one-at-a-time per above), HTTP
  `/ISAPI/ContentMgmt/download` ~19s + proprietary `IMKH` container, and the
  `.../picture` endpoint **ignores** `starttime`/`playbackURI` and always returns
  the LIVE frame. So thumbnails are slow (that's why they're lazy-loaded, low-res,
  cached, and only 1/event). **Open item:**
  the DVR can be told to capture a JPEG on each motion event (`<SaveImg>` in the
  record schedule) and store it on the DVR — that would give fast, DVR-native
  event thumbnails covering all events; not yet wired up.
- **Frontend note:** per-tile actions are `Live | Save | ⚙`; the ⚙ menu holds
  "Motion detection area", "Event log", and "Hide this camera" (the old ✕).
- **Active/inactive (pause refresh) toggle.** Each tile has an **active checkbox**
  and each device header a **group checkbox** (`data-devactive`). **Toggling is a
  targeted DOM update — NEVER a full `renderDevices()`** (`toggleActive` /
  `toggleDeviceActive` / `toggleGroupActive` → `applyTileState`/`updateCollapse`):
  a re-render does `root.innerHTML=…`, which destroys every tile's `<img>` and
  re-grabs them all, so pausing camera A would blank B/C for seconds. `buildTiles`
  keeps all tiles in the DOM even when all are paused (just adds `.collapsed` to hide
  the grid) so a per-tile toggle can re-show one without a re-render. A **paused**
  (inactive) tile shows **no frame** and is excluded from **every** refresh loop
  (`refreshAll`, `refreshMotionTiles`, motion capture, initial load) — the loops
  iterate `activeChannels(d)` (= `visibleChannels` minus `d.inactive`) instead of
  `visibleChannels`, to save DVR/RTSP bandwidth. When **all** of a device's cameras
  are paused the grid gets `.collapsed` (**header only**); re-enable via the group
  checkbox (`toggleDeviceActive` sets `d.inactive` to all-ids / `[]`). State is the
  persisted per-device **`inactive`** id list (mirrors `hidden`; masked + saved via
  `PUT /devices/<id> {inactive:[…]}`), distinct from `hidden` (which removes the
  tile entirely). `hidden` = gone; `inactive` = present but not refreshed.
- **CRUD must not disturb other tiles (frame cache).** Add/Edit/Delete/reset-hidden
  still go through `loadDevices()` → `renderDevices()` (which `root.innerHTML=…`s the
  whole list — the structure genuinely changes), but a full re-render would otherwise
  **blank and re-grab every camera** (and trip RTSP 453). So `captureTile` records each
  successful frame in a module-level **`frameCache`** (`"devId|chId"` → `{url,text,cls}`,
  in-memory only, dropped on reload — a perf cache, never a data source). Every
  **initial-fill** grab site (`renderDevices` streams, `loadChannels` DVR+RTSP,
  `resetHidden`) calls **`showTile`** = `restoreTile` (paint the cached frame instantly,
  zero network) **or** `captureTile` if the tile is new. So deleting/editing/adding one
  camera leaves all others visually untouched and un-regrabbed; only genuinely new tiles
  grab. The periodic `refreshAll`/motion loops still call `captureTile` directly (they
  must hit the DVR). `dropDeviceFrames(id)` is called on **edit** (so the changed device
  re-grabs fresh from its new host/path) and **delete** (release its blob URLs); other
  devices' cache entries are never touched.
- **Anamorphic / squished streams.** ffmpeg (`live.open_mjpeg` + `grab_still`)
  outputs **square pixels** via `_SQUARE_PIXELS` (`scale='trunc(iw*sar/2)*2':ih,setsar=1`):
  it reads the source's **sample aspect ratio** and corrects the width, so a stream
  with non-square pixels (SAR≠1 — D1/analog, some RTSP cams) isn't shown stretched
  (a JPEG/`<img>` carries no SAR to fix it downstream). SAR unknown → treated as 1
  → square sources unchanged. Verified: DVR main stays 1920×1080, 480w thumb = 480×270.

## One device model; ISAPI on/off decides DVR vs RTSP-only

Every device shares the **same fields** and the **same Edit dialog**:
`host`, `port` (labeled **ISAPI port**), `isapi_enabled` (checkbox), `rtsp_port`,
`agentgreen_port` (default **8090**) + its own `agentgreen_enabled` checkbox
(**off by default** — not everyone uses it; currently just a stored/labeled port,
no protocol), `user`, `password`, and (RTSP-only) `path`. `config.is_isapi(d)` =
explicit `isapi_enabled` else default (kind=rtsp / legacy `rtsp_url` → off, else
on); `is_rtsp = not is_isapi`.

- **Add DVR** → ISAPI on: full Hikvision device (discovery, snapshots via
  `/picture`, motion, diagnose, event log, reboot).
- **Add RTSP stream** → paste a **URL**; `config._parse_rtsp_url` splits it into
  `host`/`rtsp_port`/`user`/`password`/`path` (**no opaque `rtsp_url` stored** —
  every field is then editable in the standard dialog). The `path` is taken
  **verbatim** (NOT assumed to be `/Streaming/Channels/<id>`) and is the camera +
  its default name. Userinfo ends at the **LAST `@`** so a password with `@`/`:`/`#`
  parses right.
- **Turning ISAPI off** on any device makes it RTSP-only (needs a `path`). Then:
  `discover_channels` is bypassed — server returns **one synthetic channel**
  `{"id":"rtsp","input":"1","name":<path>}` (`_handle_rtsp_get`); the tile still +
  **Save** are one ffmpeg frame (`live.grab_still`); **Live** uses
  `live.rtsp_url` composed as `rtsp://user:pass@host:rtsp_port<path>`; **no**
  motion/diagnose/event-log/reboot (frontend hides them, `/motion/state`
  short-circuits unsupported, `events` skips it).
- **Back-compat:** a device still holding a legacy opaque `rtsp_url` works —
  `live.rtsp_url` returns it verbatim, `_rtsp_path`/mask parse a display path;
  editing it (or re-pasting a URL) migrates it to fields.
- **Dead-stream UX:** ffmpeg gets `-timeout 10000000` (**`-timeout`**, µs — this
  build has neither `-stimeout` nor `-rw_timeout`, both error) so a stream whose
  RTSP handshake answers but delivers no media (RTP blocked by NAT / wrong path)
  fails in ~10s, not a silent hang; `server._rtsp_reason` maps the ffmpeg stderr
  to a short cause (timeout→"no video", 401→auth, 404→path, refused→connect,
  "does not contain any stream"→"no video track").
- **Audio-only streams (no `m=video`).** Some links serve **only audio** (G.711
  pcm_mulaw) + ONVIF metadata and **no video track** (encoder off / no camera /
  account lacks video rights). ffmpeg's still grab then fails with "Output file
  does not contain any stream" (`live.no_video`). Instead of erroring, `/snapshot`
  for an RTSP device returns **`200 {"audio_only": true}`**; the frontend
  (`captureTile` → `markAudioOnly`) flips that stream to **audio-only mode**: the
  tile shows a "🔊 Audio only" placeholder (no more frame grabs), **Save** is
  hidden, and the Live button ("Listen") plays the sound — `GET /audio` streams
  `live.open_audio` (RTSP→**MP3** via ffmpeg `-vn -c:a libmp3lame -f mp3`) into an
  `<audio>` in the Live overlay. The flag persists as the device's **`audio_only`**
  (masked `audioOnly`); **editing an RTSP stream clears it** so it re-probes (video
  may have returned). Detection is **runtime**, not at add/import time (no probe on
  add). DVR (ISAPI) channels are unaffected — they snapshot via `/picture`, not ffmpeg.

## Groups (named containers for devices)

A **group** is a named container that nests device sections under one header
(header **＋ Create group**). Any device — DVR or RTSP — can join a group via the
**Group** field on its Add/Edit form (or the group header's **+ DVR / + RTSP**
buttons, which pre-fill the group). Groups are stored **separately** in
`config._state["groups"]` (a name list) so an **empty** group persists;
`list_groups()` merges that list with any group a device still references. A
device's membership is its `group` field (masked + persisted via
`PUT /devices/<id> {group}`; `""` = ungrouped). Server: `GET /groups`,
`POST /groups {name}`, `DELETE /groups/<name>` (delete un-assigns members —
`config.delete_group`). **Rendering by kind** (`renderDevices`):
- A **DVR** member renders as its own nested `deviceSectionHTML(d)` section
  (dhead + grid), byte-identical to a top-level one.
- An **RTSP stream is a single camera, not a device**, so it renders as a **bare
  tile** (`streamTileHTML(d)`, `data-grid=<device id> data-tile="rtsp"`) laid out
  with the group's other streams in **one shared `.streamgrid`** (a line of
  cameras) — no per-stream header. Its Edit/Delete/Remove-from-group live in the
  tile's ⚙ menu (`wireStreamTiles`); its one channel is synthesized client-side
  (`chans[id]=[{id:'rtsp'…}]`) so `captureTile`/refresh find its img by
  `data-grid` (do NOT call `buildTiles` on a stream — it'd nest a tile in a tile).
  Ungrouped streams get their own top-level `.streamgrid`.
The group header has a group-level active checkbox (`toggleGroupActive`). When a
group is turned off (all members paused) its body **folds to just the header**:
`syncGroupToggle` toggles `.collapsed` on the group's `.gmembers` (not the per-device
`updateCollapse`, which bails on `kind=rtsp` and only sees individual DVR grids — a
group of RTSP streams shares one `.streamgrid` with no per-device grid to collapse).
This fires on the group checkbox and on any per-tile/device toggle that empties the
group, and on initial render (so a persisted all-paused group renders folded).
**Re-activating a group shows tiles first, then fills them** (3 passes in
`toggleGroupActive`): (1) flip `inactive` + `applyTileState(d,ch,false)` — DOM only, no
grab — so every tile appears at once (empty, "…") and the body un-collapses
**synchronously**; (2) `showTile` each active channel (cache-restore or grab), filling
in as frames arrive; (3) `persistInactive` in the **background** (not awaited). The old
code un-collapsed only *after* `await Promise.all(persistInactive)`, so while the server
was busy with the slow RTSP snapshot grabs the whole group stayed `display:none` for
~5s then popped in at once — `applyTileState(…, capture=false)` + background persist fix
that.
**Add RTSP stream** takes **multiple URLs** in a single 3-line `<textarea id="dUrls">`
(paste many, delimited by any mix of **new lines / commas / spaces** — `parseUrls()`
splits on `/[\s,]+/`), no name — each name defaults to `host+path`; `saveDevice` POSTs
one device per URL.

## Architecture / structure to follow

```
camera_viewer.py            # thin launcher -> cameraviewer.cli.main()
cameraviewer/
  config.py                 # device store + JSON persistence (~/.camera_viewer.json, 0600); unified device model (is_isapi/is_rtsp, _parse_rtsp_url -> host/rtsp_port/user/password/path, agentgreen_port); named groups (list/create/delete_group); app settings (save_path, motion_popup); device-file import/export
  camera.py                 # ISAPI HTTP (get/put/open_stream), snapshot, MAX_STILL_RES, channel discovery, safe XML parse
  motion.py                 # gridMap codec + get_motion / set_motion (read-modify-write)  [detection AREA editor]
  events.py                 # motion ALERT stream: per-device daemon watches /ISAPI/Event/notification/alertStream, tracks per-channel VMD state (hold window) for the live badge/popup (skips kind=rtsp). get_state / start_all / ensure / stop
  store.py                  # server-side snapshot saving to config save_path (save_snapshot = ISAPI still; save_bytes = pre-grabbed bytes, used for RTSP)
  diagnose.py               # per-device health check + safe auto-fix. Live channels: motion enabled/area, VMD email+center linkage, motion-rec 10s pre/post, max-res. Unused channels (NO VIDEO input or hidden tile): only "recording left on" -> disable track. Device-wide: SMTP + DVR clock (System/time) skew -> set correct local time
  live.py                   # RTSP->MJPEG via ffmpeg (Live view); rtsp_url (stored URL for kind=rtsp) + check + open_mjpeg + grab_still (one frame, for RTSP tiles/Save); -timeout so dead streams fail fast; SAR->square-pixel so streams aren't squished
  playback.py               # recorded playback: one still at a chosen time via RTSP tracks + ffmpeg; to_span + playback_url + grab_frame (serialized to 1 RTSP session, retries RTSP 453, in-memory frame cache) + rtsp_priority() (clip playback pauses grabs)
  recordings.py             # motion event log + clip video: list_events (CMSearch POST /ISAPI/ContentMgmt/search on the motion track) + clip_process (RTSP playback -> ffmpeg fragmented MP4, audio dropped)
  scan.py                   # rtsp-scan: windowed probe (window of --parallel IPs, port-major) + ONE shared budget of `workers` slots for probe+verify combined (a found port hands its slot from probing to verifying; total in-flight <= workers) — scan_and_verify + probe_rtsp + find_credential(_ex) + enumerate_streams (vendor+channel enumeration + credential/attempts/reason) + probe_streams (streams-only wrapper) + expand_range/expand_ranges + rtsp_link + device_entry
  vendors/                  # one module per camera/DVR family (Vendor: realm keywords + per-stream path groups + channel walk w/ early-stop). __init__.detect(realm)/enumeration_order(); add a device here, not in scan.py
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
`diagnose` depends on `camera`+`motion`. `playback` depends on `live`.
`recordings` depends on `camera`+`live`+`playback`.
`server` depends on `config`/`camera`/`motion`/`events`/`store`/`diagnose`/`live`/`playback`/`recordings`/`web`.
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
| GET | `/groups` | list group names (incl. empty ones) |
| POST | `/groups` `{name}` | create an (empty) group |
| DELETE | `/groups/<name>` | delete a group (members become ungrouped) |
| POST | `/devices` | add device — DVR (`host`+`port`+…) **or** RTSP (`rtsp_url`, parsed into fields); optional `group`/`isapi_enabled`/`agentgreen_port`/`path` |
| PUT | `/devices/<id>` | update (name/host/port/rtsp_port/agentgreen_port/user/password/isapi_enabled/path/rtsp_url(re-parse)/group/hidden/inactive) |
| DELETE | `/devices/<id>` | remove device |
| GET | `/channels?device=<id>` | discover cameras |
| GET | `/snapshot?device=<id>&ch=<cid>&res=<WxH>` | one JPEG (RTSP stream with no video track → `200 {"audio_only":true}`) |
| GET | `/motion?device=<id>&input=<n>` | current motion config (detection area) |
| POST | `/motion` `{device,input,cells,sensitivity}` | save motion area |
| GET | `/motion/state?device=<id>` | live per-channel motion state `{ok,supported,message,channels:{input:bool}}` (from the alert stream) |
| GET | `/settings` | app settings `{save_path, motion_popup}` |
| PUT | `/settings` `{save_path?, motion_popup?}` | update settings (path `~`-expanded/absolute; `motion_popup` bool) |
| POST | `/save` `{device,ch}` | save a max-res (720p) JPEG to `save_path`, returns `{ok,path}` |
| POST | `/reboot` `{device}` | reboot the whole device (`PUT /ISAPI/System/reboot`); per-device, drops all streams ~1 min |
| GET | `/playback?device=<id>&ch=<track>&time=<ISO>` | one max-res still from the recording at a time |
| GET | `/events?device=<id>&ch=<track>&hours=<n>` | motion event log (CMSearch over the last n hours) → `{events:[{time,seconds,start,end}]}` |
| GET | `/clip?device=<id>&ch=<track>&start=&end=` | one motion clip as MP4 (RTSP playback→ffmpeg, streamed) |
| GET | `/live/check?device=<id>` | pre-flight: ffmpeg present + RTSP port reachable |
| GET | `/live?device=<id>&ch=<cid>&stream=main\|sub` | live MJPEG stream (RTSP→ffmpeg), runs until the browser disconnects |
| GET | `/audio?device=<id>&ch=<cid>` | audio-only stream as MP3 (RTSP→ffmpeg `-vn -f mp3`), for the "Listen" player on a no-video stream |

## Non-negotiable invariants

1. **Stdlib only for Python deps.** No third-party *Python packages*, ever. The
   one allowed **external binary** is **ffmpeg**, used solely by `live.py` for the
   real-time Live view (RTSP→MJPEG) while a Live window is open. The dashboard
   gallery stays on stdlib JPEG snapshots. Don't add other binaries or any pip
   package without discussing.
2. **Passwords never reach the browser.** `config.mask()` strips them; the
   browser proxies snapshots through the server. Never add an endpoint that
   returns a stored password. **A pasted RTSP URL's credentials are parsed into
   the `user`/`password` fields** on add (`_parse_rtsp_url`) and then masked like
   any password — the raw URL is not stored or returned. Config file is chmod
   `0600`, lives in `$HOME` (not the repo) so secrets aren't committed.
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
- **Empty camera slots (NO VIDEO) — every per-channel config endpoint 403s.** A
  DVR input with nothing plugged in reports `videoInputEnabled=false` /
  `resDesc=NO VIDEO` in `/ISAPI/System/Video/inputs/channels` (which lists ALL
  inputs, always readable). For that channel: `/ISAPI/Streaming/channels/<id>` and
  `.../motionDetection` and `/ISAPI/Event/triggers/VMD-<n>` all **403**, `/picture`
  **500s**, and the channel is **absent from the `/ISAPI/Streaming/channels`
  list**. But its **record track** `/ISAPI/ContentMgmt/record/tracks/<id>` is still
  GET/PUT-able and can sit `<Enable>true</Enable>` — recording an empty channel and
  wasting disk. **Which slot is empty varies per DVR:** `home` = input **4**
  (Camera 04), `garage` = input **1** (Camera 01). This is why the resolution
  "Fix" appeared to do nothing on `home` Camera 04 — the PUT to raise it to 1080p
  hit the 403 wall. Diagnose now detects NO-VIDEO inputs and, instead of the
  motion/quality checks, just offers to disable their recording.
- **Stream capabilities LIE about resolution (mirrors the snapshot 1080p quirk).**
  `garage` Camera 04 (`/ISAPI/Streaming/channels/401`) sits at **1280×720**; its
  `/capabilities` advertises `videoResolutionWidth opt="1920,…"` /
  `Height opt="1080,…"` and the video input even reports `resDesc=1080P25` — yet
  PUTting 1920×1080 returns **HTTP 500 `<statusCode>3</statusCode>` `deviceError`**
  (tried @2048 and @4096 kbps CBR — both fail; an *identity* 720p PUT succeeds, so
  the write path is fine, the encoder just won't do 1080p on that channel). So
  **720p is that channel's real ceiling** and the capability list can't be trusted.
  Lesson baked into diagnose: quality is judged by an **HD floor (≥720p)**, and the
  fix **verifies by read-back + steps down** instead of trusting a 200/ capabilities.
  Don't hammer a rejected resolution — two failed PUTs was enough to learn this.
- **DVR clock drifts badly.** `home` was ~228 min (−13699s) behind real local
  time; `timeMode=manual`, `timeZone=CST-3:00:00`, `localTime` carries a `+03:00`
  offset. Since the mode is already `manual`, writing a corrected `<localTime>`
  sticks (no NTP override). `_set_clock` computes `now(UTC)+parsed_offset`, keeps
  `timeMode`/`timeZone`, PUTs `/ISAPI/System/time` — verified: `home` went from
  −228 min to ±2s. `garage` was already in sync (−54s, within the 120s tolerance).
- **Recorded playback + why there's no device motion log.** The DVR records
  **continuously** (`DefaultRecordingMode=CMR`; segments tagged
  `recordType.meta.std-cgi.com/timing`). `POST /ISAPI/ContentMgmt/search`
  (`CMSearchDescription` → `CMSearchResult`) **works** and returns ~1h segments,
  each with a `<playbackURI>` `rtsp://<lan-ip>/Streaming/tracks/<track>/?starttime=…&endtime=…`.
  Track id = channel picture id (`101`..`401`). The LAN ip in the URI is
  unreachable externally — `playback.py` rebuilds the URL against the device host
  + forwarded **rtsp_port** and ffmpeg grabs one **1080p** frame (verified). Times
  are the DVR's wall clock formatted `YYYYMMDDThhmmssZ` (labeled Z but NOT UTC — no
  tz conversion). **Getting a real motion-event log via ISAPI is not possible on
  this OEM firmware:** `/ISAPI/ContentMgmt/logSearch` exists but rejects the
  documented `LogSearchDescription` (and every variant) with `statusCode 6
  badXmlContent` (responses carry `urn:psialliance-org` — PSIA/OEM build, schema
  undocumented, likely NET_DVR SDK-only on port 8000 which we don't do); and the
  `//metadata.ISAPI/motionDetection` search filter is *accepted* but returns the
  continuous `timing` segments, not motion (recording isn't event-based). So motion
  *times* would require switching the DVR to motion-triggered recording; we instead
  offer time-based playback over the continuous recording.
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
