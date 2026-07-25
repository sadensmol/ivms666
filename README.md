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

## Scan an IP (or range) for RTSP

Probe one host or a whole range for live RTSP ports, and — if you give it
credential lists — verify which login/password actually opens the stream:

```bash
python3 -m cameraviewer rtsp-scan --host <ip>                        # single IP, port 554
python3 -m cameraviewer rtsp-scan --range 192.168.1.0/24             # whole subnet
python3 -m cameraviewer rtsp-scan --range 192.168.1.10-20 --ports 554,8554
python3 -m cameraviewer rtsp-scan --host <ip> --logins admin,root --passwords 12345,admin --output found.json
python3 -m cameraviewer rtsp-scan                                    # use scan.range/ports from default_config.json
```

With no `--host`/`--range`/`--ports`, it reads `range`/`ports` from the `scan`
section of the bundled
[cameraviewer/default_config.json](cameraviewer/default_config.json). Both accept
a **JSON list** (scanned together, hosts de-duplicated) or a single/comma string:

```json
{"scan": {"range": ["192.168.1.0/24", "192.168.2.0/24"], "ports": ["554", "8554"]}}
```

`--range` (one spec or comma-separated) and `--ports` override the config lists.
Only scan IPs/ranges you own.

Hosts are probed **in parallel** — up to `--parallel` at a time (default 10,
capped at the number of IPs). Within each host, login/password combinations are
still tried **one at a time** (concurrent auth can trip a DVR lockout), so
progress is shown per host (`scanning N of M hosts`).

### Verifying credentials and setting up the app

Credentials default to `scan.logins`/`scan.passwords` in
[cameraviewer/default_config.json](cameraviewer/default_config.json); pass
`--logins`/`--passwords` (comma-separated) to **override** them — when provided,
only the provided values are used. For each RTSP port it finds, the scan tries
**every login with every password**, in order (login 1 with every password, then
login 2, …), one at a time — it answers the DVR's HTTP-Digest challenge
(`realm="Embedded Net DVR"`) with an authenticated `DESCRIBE` and keeps only the
combinations the device accepts, each with a ready-to-use `rtsp://user:pass@…`
link. `--user`/`--password` add one extra login/password.

Verified devices are written to `--output` (default `rtsp-scan-output.json`) as
an import-ready file. Feed it back in to set up the app:

```bash
python3 -m cameraviewer import --file found.json   # merge into ~/.camera_viewer.json
python3 camera_viewer.py                            # launch — the devices are there
```

`import` de-dupes by host + RTSP port + login, so re-running is safe. The
HTTP/ISAPI port (used for snapshots/motion) can't be discovered by an RTSP scan,
so imported devices default it to `80` — fix it in the UI if the DVR's web port
differs. Only brute-force credentials on devices you own.

### Varying different octets in `--range`

An IPv4 address is four dot-separated octets, `A.B.C.D`. Pick the range form by
how many of them you need to vary:

| Vary | Form | Example | Expands to |
|---|---|---|---|
| **Last octet only** (`D`) | dash shorthand | `192.168.1.10-20` | `192.168.1.10 … .20` |
| **Last octet, full** (`D`) | full dash | `192.168.1.10-192.168.1.20` | `192.168.1.10 … .20` |
| **Last 2 octets** (`C.D`) | CIDR `/16` | `192.168.0.0/16` | `192.168.0.0 … 192.168.255.255` |
| **Last 3 octets** (`B.C.D`) | CIDR `/8` | `10.0.0.0/8` | `10.0.0.0 … 10.255.255.255` |
| **All 4 octets** (`A.B.C.D`) | CIDR `/0` | `0.0.0.0/0` | the entire IPv4 space *(don't)* |
| **Arbitrary span** (crosses dots) | full dash | `10.0.1.250-10.0.2.5` | `…1.250 … 1.255, 2.0 … 2.5` |
| **From a host, upward** | CIDR with host bits | `192.0.0.62/24` | `192.0.0.62 … 192.0.0.255` |

The scan iterates addresses as integers, so a full dash range rolls over octet
("dot") boundaries automatically. Only the `-N` **shorthand** is limited to the
last octet. Any CIDR width works (`/22`, `/28`, …); the prefix length is just how
many leading bits stay fixed — the rest vary.

A **CIDR starts at the exact address you write** and goes up to the top of the
block, never below it — `192.0.0.62/24` scans `.62 … .255`, while `192.0.0.0/24`
(host part 0) covers the whole `.0 … .255` block. Network and broadcast addresses
are included.

> ⚠️ Size grows fast: `/24` = 256 addresses, `/16` = 65 536, `/8` = ~16 million.
> Scan the smallest range that covers your cameras.

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
- **Live motion indicator** — the moment the DVR detects motion, that camera's tile
  gets a **blinking solid red border** and the captured frame pops up full-screen.
  The server subscribes to the DVR's event alert stream (no polling of the video),
  so it works even with the browser closed. (Requires the camera's motion trigger to
  have "Notify Surveillance Center" enabled — use **Diagnose** below to check/fix it.)
- **Diagnose** — a per-device button (device header) audits each visible camera's
  motion→email→app pipeline: motion enabled + area painted, the VMD trigger's
  `email` and `center` linkages, and SMTP. It flags problems and offers one-click
  **Fix** for the repairable ones (adds the missing `email`/`center` linkage). Hidden
  cameras are skipped.
- **Save images to a folder** — the **Save** button (and every motion event) writes
  a **max-resolution (720p)** JPEG to a folder you choose in **⚙ Settings**
  (default `~/CameraViewer`, created if missing). Saving happens on the server, so
  the file lands on the machine running Camera Viewer.
- **Reboot** — a per-device button (in the device header, next to Edit/Delete)
  reboots the whole DVR/NVR (`ISAPI /System/reboot`) after a confirm; all its
  cameras drop for ~a minute and the app reconnects on its own.
- **Per-camera ⚙ menu** — holds **Motion detection area** (paint the detection
  grid + sensitivity, saved back to the device; gated by a round-trip self-check
  so it can't corrupt the grid) and **Hide this camera** (hide the tile,
  per-device and persisted). **Reset hidden** restores hidden tiles.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

Tests run fully offline (the camera network layer and ffmpeg are faked).

See [CLAUDE.md](CLAUDE.md) for architecture, invariants, and device details.
