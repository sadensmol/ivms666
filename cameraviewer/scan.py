"""Probe an IP for RTSP: send an RTSP OPTIONS request to candidate ports and
report which ones actually speak RTSP (not just an open TCP socket).

A plain TCP connect is not enough — a forwarded Hikvision SDK port (8000) opens
but ignores RTSP and waits for a binary handshake. A *real* RTSP port replies to
`OPTIONS` with `RTSP/1.0 200 OK` (open) or `RTSP/1.0 401 Unauthorized`
(auth required, realm="Embedded Net DVR"). We treat 200/401 as "RTSP found" and
build the `rtsp://.../Streaming/Channels/101` link the same way live.py does.
Pure stdlib socket — no ffmpeg needed just to detect the port.
"""

import base64
import hashlib
import ipaddress
import re
import secrets
import socket
import threading
from concurrent.futures import ThreadPoolExecutor

from . import live, vendors

STD_PORT = 554
MAX_CHANNELS = 8   # DVRs are usually 4/8/16-ch; probe channels 1..this per found port
# The per-vendor RTSP path schemas live in the `vendors` package (one module each);
# add a device there, not here.


def probe_rtsp(host, port, timeout=5):
    """Send an RTSP OPTIONS to host:port. Returns (ok, detail):
    ok=True when the reply's status line is RTSP/1.0 200 or 401; detail is the
    status line (plus realm if the server advertised one), or the failure
    reason. Anything that isn't an RTSP reply (SDK port, HTTP, refused) -> ok=False."""
    req = (
        f"OPTIONS rtsp://{host}:{port}/ RTSP/1.0\r\n"
        "CSeq: 1\r\n"
        "User-Agent: cameraviewer\r\n\r\n"
    ).encode()
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, int(port)))
        s.sendall(req)
        raw = s.recv(1024).decode("latin-1", "replace")
    except Exception as e:  # noqa: BLE001 — device/network failure -> not RTSP
        return False, f"{type(e).__name__}: {e}"
    finally:
        s.close()

    line = raw.split("\r\n", 1)[0].strip()
    # ANY `RTSP/1.0 <code>` reply proves an RTSP server is listening — the code
    # is about the requested URL, not the port. This DVR returns 404 to
    # `OPTIONS rtsp://host:port/` (root isn't a stream; real path is
    # /Streaming/Channels/<id>) yet the port is a perfectly live RTSP port.
    parts = line.split()
    if len(parts) < 2 or parts[0] != "RTSP/1.0" or not parts[1].isdigit():
        return False, f"not RTSP (got: {line!r})"
    realm = ""
    for h in raw.split("\r\n"):
        if h.lower().startswith("www-authenticate:") and "realm=" in h:
            realm = " (" + h.split("realm=", 1)[1].split(",")[0].strip() + ")"
            break
    return True, line + realm


def scan_ports(host, ports, timeout=5):
    """Probe each port; return a list of (port, detail) for the ones speaking RTSP."""
    found = []
    for port in ports:
        ok, detail = probe_rtsp(host, port, timeout)
        if ok:
            found.append((port, detail))
    return found


def expand_range(spec):
    """Expand an IP-range spec into a list of host strings. Accepts:
      - CIDR: '192.168.1.0/24' — starts at the exact address written and goes UP
        to the top of the block, never below it. '192.0.0.62/24' -> 192.0.0.62 ..
        192.0.0.255; a host part of 0 ('192.0.0.0/24') covers the whole block
        (network + broadcast addresses included on purpose).
      - dash (full): '10.0.0.5-10.0.0.9'
      - dash (last octet): '10.0.0.5-9'
      - single IP: '10.0.0.5'
    """
    spec = spec.strip()
    if "/" in spec:
        iface = ipaddress.ip_interface(spec)          # keeps the host bits (.62)
        start = int(iface.ip)                          # the address as written
        end = int(iface.network.broadcast_address)     # top of the block
        return [str(ipaddress.ip_address(i)) for i in range(start, end + 1)]
    if "-" in spec:
        start, end = (p.strip() for p in spec.split("-", 1))
        start_ip = ipaddress.ip_address(start)
        if "." not in end:  # last-octet shorthand: 10.0.0.5-9
            end = ".".join(str(start_ip).split(".")[:3] + [end])
        end_ip = ipaddress.ip_address(end)
        if end_ip < start_ip:
            raise ValueError(f"range end {end_ip} precedes start {start_ip}")
        return [str(ipaddress.ip_address(i)) for i in range(int(start_ip), int(end_ip) + 1)]
    return [spec]


def expand_ranges(specs):
    """Expand a list of range specs (each as accepted by `expand_range`) into a
    single de-duplicated, order-preserving list of host strings. Lets the config
    carry several ranges: `"range": ["192.168.1.0/24", "192.168.2.0/24"]`."""
    seen = set()
    hosts = []
    for spec in specs:
        for host in expand_range(spec):
            if host not in seen:
                seen.add(host)
                hosts.append(host)
    return hosts


def scan_hosts(hosts, ports, timeout=5, workers=64):
    """Probe every (host, port) pair concurrently. Returns an ordered list of
    (host, port, detail) for pairs that speak RTSP, sorted by input order."""
    pairs = [(h, p) for h in hosts for p in ports]

    def _probe(pair):
        host, port = pair
        ok, detail = probe_rtsp(host, port, timeout)
        return (host, port, detail) if ok else None

    with ThreadPoolExecutor(max_workers=min(workers, len(pairs) or 1)) as ex:
        results = list(ex.map(_probe, pairs))
    return [r for r in results if r]


def rtsp_link(host, port, path="/Streaming/Channels/101", user=None, password=None):
    """Build the rtsp:// link for a discovered port + stream `path`, reusing
    live.rtsp_url so URL/credential encoding stays identical."""
    cfg = {"kind": "rtsp", "host": host, "rtsp_port": port,
           "user": user, "password": password, "path": path}
    return live.rtsp_url(cfg, "rtsp")


def _realm_from_detail(detail):
    """The digest realm from a probe_rtsp detail line, e.g.
    'RTSP/1.0 401 Unauthorized ("Embedded Net DVR")' -> 'Embedded Net DVR'."""
    m = re.search(r"\(([^)]*)\)\s*$", detail or "")
    return m.group(1).strip().strip('"') if m else ""


# --- credential verification ------------------------------------------------
# Detecting an RTSP port (above) needs no auth. Confirming which login/password
# actually opens a stream does: we DESCRIBE the real stream path, answer the
# device's 401 challenge (HTTP Digest, per RFC 2617 — this DVR's realm is
# "Embedded Net DVR"; Basic is also handled), and treat a final 200 as "these
# credentials work". Pure stdlib (socket + hashlib) — no ffmpeg/urllib needed.


def credential_combos(logins, passwords):
    """Every (login, password) pair — the brute list the scan verifies. Blank
    entries are dropped so an empty list on either side yields no combos."""
    logins = [u for u in logins if u]
    passwords = [p for p in passwords if p]
    return [(u, p) for u in logins for p in passwords]


def _parse_challenge(header):
    """Parse a WWW-Authenticate value into (scheme, {param: value}). Handles both
    quoted (realm="x") and bare (qop=auth) params."""
    header = header.strip()
    scheme = header.split(None, 1)[0].lower() if header else ""
    rest = header[len(scheme):]
    params = {}
    for m in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', rest):
        params[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    return scheme, params


def _md5(s):
    return hashlib.md5(s.encode()).hexdigest()


def _digest_authorization(user, password, method, uri, params, cnonce=None):
    """Build a 'Digest ...' Authorization value for the given challenge params."""
    realm = params.get("realm", "")
    nonce = params.get("nonce", "")
    ha1 = _md5(f"{user}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    fields = {"username": user, "realm": realm, "nonce": nonce, "uri": uri}
    qop = params.get("qop")
    if qop:
        qop = qop.split(",")[0].strip()  # take the first offered token (e.g. "auth")
        nc = "00000001"
        cnonce = cnonce or secrets.token_hex(8)
        fields["response"] = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
        fields.update({"qop": qop, "nc": nc, "cnonce": cnonce})
    else:
        fields["response"] = _md5(f"{ha1}:{nonce}:{ha2}")
    if params.get("opaque"):
        fields["opaque"] = params["opaque"]
    # qop/nc are sent unquoted per RFC 2617; everything else is quoted.
    bare = {"qop", "nc"}
    parts = ", ".join(f"{k}={v}" if k in bare else f'{k}="{v}"' for k, v in fields.items())
    return "Digest " + parts


def _authorization(user, password, method, uri, challenge):
    """Answer a WWW-Authenticate challenge; '' if the scheme is unsupported."""
    scheme, params = _parse_challenge(challenge)
    if scheme == "digest":
        return _digest_authorization(user, password, method, uri, params)
    if scheme == "basic":
        return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    return ""


def _recv_head(sock, limit=65536):
    """Read an RTSP reply up to the end of its headers (blank line)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk or len(buf) > limit:
            break
        buf += chunk
    return buf.decode("latin-1", "replace")


def _status(resp):
    """(code, status-line). code is the int after 'RTSP/1.0', or None."""
    line = resp.split("\r\n", 1)[0].strip()
    parts = line.split()
    code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
    return code, line


def _www_authenticate(resp):
    for h in resp.split("\r\n"):
        if h.lower().startswith("www-authenticate:"):
            return h.split(":", 1)[1].strip()
    return ""


def rtsp_describe(host, port, user, password, path="/Streaming/Channels/101", timeout=5):
    """DESCRIBE a specific stream `path` with digest/basic auth. Returns (code, detail):
    code is the final RTSP status — **200** = credentials accepted AND the path is a
    real stream; **404** = credentials accepted but that path doesn't exist (wrong
    schema); **401** = credentials rejected; None = connection/socket error."""
    url = f"rtsp://{host}:{port}{path}"

    def _send(sock, cseq, auth=None):
        lines = [f"DESCRIBE {url} RTSP/1.0", f"CSeq: {cseq}",
                 "User-Agent: cameraviewer", "Accept: application/sdp"]
        if auth:
            lines.append(f"Authorization: {auth}")
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, int(port)))
        _send(s, 1)
        resp = _recv_head(s)
        code, line = _status(resp)
        if code == 401:  # answer the challenge on the same (persistent) connection
            auth = _authorization(user, password, "DESCRIBE", url, _www_authenticate(resp))
            if not auth:
                return code, "unsupported auth scheme"
            _send(s, 2, auth)
            resp = _recv_head(s)
            code, line = _status(resp)
        return code, line
    except Exception as e:  # noqa: BLE001 — device/network failure -> creds not verified
        return None, f"{type(e).__name__}: {e}"
    finally:
        s.close()


def _chunks(seq, size):
    for i in range(0, len(seq), max(1, size)):
        yield seq[i:i + size]


def scan_and_verify(hosts, ports, creds, timeout=5, workers=10, verify_workers=None,
                    on_found=None, on_attempt=None, on_verified=None, on_host_done=None):
    """Scan a **window of up to `workers` IPs at a time, PORT BY PORT**: probe port 1
    on all IPs in the window in parallel, then port 2 on the same IPs, … up to the
    last configured port; then advance to the next window of IPs.

    **Verification runs on a SEPARATE pool** (`verify_workers`, default = `workers`):
    the instant a port answers RTSP, its credential + stream probing (`probe_streams`
    — which can be slow: many logins × vendor paths × channels) is handed to the
    verify pool and the scan **keeps probing IPs** — a single slow/hanging device
    never freezes the whole scan. Credentials for one host are still tried one at a
    time (no per-host lockout); different hosts verify concurrently.

    Live callbacks (the caller serializes output):
      `on_found(host, port, detail)`  — the instant an RTSP port is detected
      `on_attempt(host, port, i, n, user, password)` — before each login attempt
      `on_verified(hit)`              — after that port's streams are enumerated
      `on_host_done(done, total, hits)` — as each host's PROBING finishes (progress)

    Returns a flat list of hit dicts:
      {"host", "port", "detail", "streams": [(user, password, path, vendor), ...]}
    — one entry per working camera/stream on that port.
    """
    total = len(hosts)
    win = max(1, min(workers, total))
    verify_workers = verify_workers if verify_workers is not None else win
    all_hits = []
    lock = threading.Lock()
    done = [0]

    def _verify(host, port, detail):
        attempt_cb = ((lambda i, n, u, p: on_attempt(host, port, i, n, u, p)) if on_attempt else None)
        streams = probe_streams(host, port, creds, realm=_realm_from_detail(detail),
                                timeout=timeout, on_attempt=attempt_cb)
        hit = {"host": host, "port": port, "detail": detail, "streams": streams}
        if on_verified:                       # (fired outside `lock` -> no nested-lock deadlock)
            on_verified(hit)
        with lock:
            all_hits.append(hit)

    with ThreadPoolExecutor(max_workers=win) as probe_ex, \
            ThreadPoolExecutor(max_workers=max(1, verify_workers)) as verify_ex:
        futs = []
        for window in _chunks(hosts, win):
            for port in ports:                              # port-major within the window
                probed = list(probe_ex.map(
                    lambda h, _pt=port: (h, *probe_rtsp(h, _pt, timeout)), window))
                for host, ok, detail in probed:
                    if not ok:
                        continue
                    if on_found:
                        on_found(host, port, detail)
                    if creds:                               # verify in the background; DON'T block probing
                        futs.append(verify_ex.submit(_verify, host, port, detail))
                    else:
                        with lock:
                            all_hits.append({"host": host, "port": port, "detail": detail, "streams": []})
            for _ in window:                                # window probed -> advance host progress now
                with lock:
                    done[0] += 1
                    d = done[0]
                if on_host_done:
                    on_host_done(d, total, [])
        for f in futs:                                      # drain outstanding verifications
            f.result()
    return all_hits


def device_entry(host, rtsp_port, user=None, password=None, path="/Streaming/Channels/101"):
    """An import-ready entry for a verified stream, in the current config shape: a
    single `rtsp_url` that `import` parses into an RTSP-only device (host/rtsp_port/
    user/password/path). Credentials, if any, are embedded in the URL; `path` is the
    verified stream path. Written by `rtsp-scan --output`, consumed by `import`."""
    return {"rtsp_url": rtsp_link(host, rtsp_port, path, user=user, password=password)}


def find_credential(host, port, creds, probe_path="/Streaming/Channels/101",
                    timeout=5, on_attempt=None):
    """Find ONE accepted credential for a known-RTSP host:port. Tries each
    (user, password) in order, ONE at a time (sequential -> no failed-login
    lockout), stopping at the first that works. A credential is "accepted" the
    moment answering the 401 challenge yields anything but another 401 (200 = valid
    path, 404 = valid login / wrong path). Returns (user, password) or None.
    `on_attempt(i, n, user, password)` (1-based) fires before each try."""
    total = len(creds)
    for i, (user, password) in enumerate(creds, 1):
        if on_attempt:
            on_attempt(i, total, user, password)
        code, _ = rtsp_describe(host, port, user, password, probe_path, timeout)
        if code is None:
            return None                 # connection died -> stop trying this port
        if code != 401:                 # got past auth -> this login works
            return (user, password)
    return None


def probe_streams(host, port, creds, realm="", timeout=5, on_attempt=None,
                  max_channels=MAX_CHANNELS):
    """Enumerate every working stream on a found RTSP port, vendor- and
    channel-aware:
      1. infer the vendor from the 401 `realm` (`vendors.detect`);
      2. find ONE working credential (sequential; `on_attempt` per try);
      3. with it, walk vendors in best-guess order (`vendors.enumeration_order`) and
         let each enumerate its channels (stopping at the first empty channel) —
         the first vendor that yields any stream wins.
    Returns [(user, password, path, vendor_name), …] — one per camera/stream."""
    guess = vendors.detect(realm)
    probe_path = guess.probe_path() if guess else "/Streaming/Channels/101"
    got = find_credential(host, port, creds, probe_path, timeout, on_attempt)
    if not got:
        return []
    user, password = got

    def describe(path):
        return rtsp_describe(host, port, user, password, path, timeout)[0]

    for vendor in vendors.enumeration_order(realm):
        paths = vendor.streams(describe, max_channels)
        if paths:
            return [(user, password, p, vendor.name) for p in paths]
    return []
