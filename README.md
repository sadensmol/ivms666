# ivms666

A tiny **zero-dependency** (Python stdlib only — no `pip`/`brew`) local GUI for
Hikvision-style DVR/NVRs. Runs a small local web server and opens your browser.

## Run

```bash
python3 ivms666.py
```

Then in the browser, add a device two ways (both use the same fields —
host, ISAPI port + an **ISAPI enabled** checkbox, RTSP port, agentgreen port,
username, password):
- **+ Add DVR** → for a Hikvision-style ISAPI DVR (snapshots, motion, recording,
  event log, diagnose).
- **+ Add RTSP stream** → paste a **full RTSP URL** (`rtsp://user:pass@host:554/path`);
  it's split into the fields above (all editable afterwards in the standard Edit
  dialog). The URL's path (e.g. `/Streaming/Channels/101`) is the camera. RTSP-only
  devices do Live + snapshots via ffmpeg; motion/recording/event-log need ISAPI.

Turn the **ISAPI enabled** checkbox off on any device to work over RTSP only.
Group devices together with **+ Create group**.

Credentials and your view setup are saved to `~/.ivms666.json` (chmod 600)
and restored on the next launch. Passwords stay on the server — the browser
never receives them (a pasted RTSP URL's credentials are parsed into the
password field, never sent back to the browser).

List cameras from the terminal instead:

```bash
python3 ivms666.py discover --host <camera-ip> --port 80 \
    --user admin --password 'secret'
```

## Scan an IP (or range) for RTSP

Probe one host or a whole range for live RTSP ports, and — if you give it
credential lists — verify which login/password actually opens the stream:

```bash
python3 ivms666.py rtsp-scan --host <ip>                        # single IP, port 554
python3 ivms666.py rtsp-scan --range 192.168.1.0/24             # whole subnet
python3 ivms666.py rtsp-scan --range 192.168.1.10-20 --ports 554,8554
python3 ivms666.py rtsp-scan --host <ip> --logins admin,root --passwords 12345,admin --output found.json
python3 ivms666.py rtsp-scan                                    # use scan.range/ports from default_config.json
```

With no `--host`/`--range`/`--ports`, it reads `range`/`ports` from the `scan`
section of the bundled
[default_config.json](default_config.json). Both accept
a **JSON list** (scanned together, hosts de-duplicated) or a single/comma string:

```json
{"scan": {"range": ["192.168.1.0/24", "192.168.2.0/24"], "ports": ["554", "8554"]}}
```

`--range` (one spec or comma-separated) and `--ports` override the config lists.
Only scan IPs/ranges you own.

The scan is **pipelined with live output**: a pool of `--parallel` workers
(default 10) hunts for RTSP across the range, and the moment a worker finds a
port it **verifies that port's login/password right there** while the other
workers keep scanning. The terminal shows a live view: each `✓ RTSP host:port`
stays as a permanent line, and directly below the hosts still being checked a
line updates in place — `host:port  trying user:pass (i/n)` — with a
`N/M hosts · X RTSP · Y verified` line at the bottom; a solved port prints a
permanent `→ login OK …`. Credentials are still tried **one at a time within a
host** (concurrent auth can trip a DVR lockout). Piped output stays plain; Ctrl+C
stops it cleanly.

### Verifying credentials and setting up the app

Credentials default to `scan.logins`/`scan.passwords` in
[default_config.json](default_config.json); pass
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
python3 ivms666.py import --file found.json   # merge into ~/.ivms666.json
python3 ivms666.py                            # launch — the devices are there
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

### Known ranges

| Range | Owner |
|---|---|
| `85.172.0.0 – 85.175.255.255` | Rostelecom |

> Only scan IPs/ranges you own or are authorized to test.

## Features

- **Multiple cameras/NVRs**, each with saved credentials.
- **Auto channel discovery** and a live snapshot gallery (one image per camera).
- **Pause/activate cameras** — a checkbox per tile (and a group checkbox per device)
  turns a camera **inactive**: it stops refreshing and shows no frame, so you don't
  spend DVR/RTSP bandwidth on cameras you're not watching. Pause every camera in a
  device and it collapses to just its header. Remembered across restarts.
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
- **Diagnose** — a per-device button (device header) audits each camera's
  motion→email→app pipeline: motion enabled + area painted, the VMD trigger's
  `email` and `center` linkages, SMTP, **recording** (motion-triggered with
  10s pre/post-record), **recording quality** (main stream ≥HD, raised to the
  highest resolution the DVR truly accepts), and the **DVR clock** (fixes a wrong
  clock so recording/e-mail timestamps are right). Cameras you've **hidden**, and
  inputs with **no camera** (empty NO-VIDEO slots), skip the full check — instead
  it flags any recording still left ON for them and offers to **turn it off to save
  disk space**. One-click **Fix** repairs all the fixable ones.
- **Save images to a folder** — the **Save** button (and every motion event) writes
  a **max-resolution (720p)** JPEG to a folder you choose in **⚙ Settings**
  (default `~/ivms666`, created if missing). Saving happens on the server, so
  the file lands on the machine running ivms666.
- **Event log** — per-camera ⚙ → "Event log": lists the DVR's **motion events**
  (each a motion-triggered recording with 10s before/after), newest first, with
  time and duration. Each row shows **thumbnail frames** of that event (a small
  timeline across the ±10s window — click one to enlarge). **▶ Play** streams the
  clip into a video player with native play/pause/seek/rewind and a speed selector.
  Needs the DVR set to motion recording (use **Diagnose** to enable it) and ffmpeg
  + the forwarded RTSP port for Play. If the list is empty, recording isn't
  motion-triggered yet.
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

---

## Deployment — `ivms666.sadensmol.com`

Push to `main` → GitHub runs the tests, builds `ghcr.io/sadensmol/ivms666`, and
redeploys it on a DigitalOcean droplet. The droplet keeps **no inbound web port
open**: a `cloudflared` sidecar dials out to Cloudflare, and Cloudflare Access
gates the hostname with e-mail one-time PINs.

The app has **no login of its own** — Access is the only thing between the
internet and your cameras. That is why the app container publishes no port.

### 1. GitHub secrets (Settings → Secrets and variables → Actions)

Generate a dedicated deploy key on your Mac:

```bash
ssh-keygen -t ed25519 -C 'gh-actions-ivms666' -f ~/.ssh/ivms666_deploy -N ''
pbcopy < ~/.ssh/ivms666_deploy       # private key -> DROPLET_SSH_KEY
cat ~/.ssh/ivms666_deploy.pub        # public key  -> droplet authorized_keys
```

| Secret | Value |
|---|---|
| `DROPLET_HOST` | droplet IPv4 |
| `DROPLET_USER` | `root`, or a `deploy` user that is in the `docker` group |
| `DROPLET_SSH_KEY` | the whole **private** key, including the BEGIN/END lines |

That is the complete list. **No DigitalOcean API token** is needed (the droplet
already exists and CI never calls the DO API), and **no registry credentials** —
the repo is public, so the GHCR package is public and the droplet pulls
anonymously.

### 2. Droplet, once

```bash
ssh root@<droplet-ip>
curl -fsSL https://get.docker.com | sh          # docker + compose plugin

mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo 'ssh-ed25519 AAAA... gh-actions-ivms666' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys

ufw default deny incoming                       # the tunnel is outbound-only:
ufw allow 22/tcp                                # nothing needs 80/443 open
ufw enable

mkdir -p /opt/ivms666
```

### 3. Cloudflare Tunnel

1. Zero Trust → **Networks → Tunnels → Create a tunnel** → *Cloudflared*, name it
   `ivms666`.
2. Copy the tunnel **token** and put it on the droplet — never in GitHub:

   ```bash
   printf 'TUNNEL_TOKEN=%s\n' 'eyJhIjoi...' > /opt/ivms666/.env
   chmod 600 /opt/ivms666/.env
   ```

3. In the tunnel's **Public Hostnames** tab: subdomain `ivms666`, domain
   `sadensmol.com`, service **HTTP** → `app:8777` (the compose service name, not
   an IP). The `CNAME` is created for you — don't add one by hand.

### 4. Cloudflare Access (the e-mail gate)

Zero Trust → **Access → Applications → Add an application → Self-hosted**:

- Application domain: `ivms666.sadensmol.com`
- Policy: **Allow**, rule *Emails* → every address that may watch the cameras
- Login methods: **One-time PIN**

Verify in an incognito window: the Cloudflare e-mail prompt must appear *before*
any camera page.

### 5. First deploy

Push to `main` (or run the workflow manually), then on the droplet:

```bash
cd /opt/ivms666 && docker compose ps      # app + cloudflared up
docker compose logs -f app
```

Open the site, pass the e-mail PIN, and add your devices through the UI. Config
lives in the `ivms666_cv-data` volume (`~/.ivms666.json`, chmod 0600) and survives
redeploys — nothing is baked into the image.

### What this deployment cannot fix

- **The droplet must reach the DVR over the internet** — its ISAPI and RTSP ports
  forwarded to a static IP or DDNS name. A LAN-only DVR shows an empty dashboard.
- **The DVR serves roughly one concurrent RTSP session.** The cloud instance now
  competes with anyone watching at home; expect `453 Not Enough Bandwidth` on
  clips and event thumbnails while both are live.
- **DVR credentials live on the droplet** (0600, inside the volume) so the app can
  authenticate — the same trust step as forwarding the ports in the first place.

### Rollback

Every build is tagged with its commit SHA as well as `latest`, so an older image
can be pinned in `docker-compose.yml` temporarily. Simpler in practice: re-run the
workflow (`workflow_dispatch`) from the earlier commit — it rebuilds `latest` from
that tree and redeploys. A hand-pinned tag is overwritten by the next push to
`main`, so treat pinning as temporary.

---

See [CLAUDE.md](CLAUDE.md) for architecture, invariants, and device details.
