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
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import live

STD_PORT = 554


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


def rtsp_link(host, port, channel_id="101", user=None, password=None):
    """Build the rtsp:// link for a discovered port (main stream, channel 101),
    reusing live.rtsp_url so URL/credential encoding stays identical."""
    cfg = {"host": host, "rtsp_port": port, "user": user, "password": password}
    return live.rtsp_url(cfg, channel_id)


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


def rtsp_describe(host, port, user, password, channel_id="101", timeout=5):
    """DESCRIBE the stream path with digest/basic auth. Returns (code, detail):
    code is the final RTSP status (200 = credentials accepted), detail the status
    line or the failure reason (None code -> connection/socket error)."""
    url = f"rtsp://{host}:{port}/Streaming/Channels/{channel_id}"

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


def scan_and_verify(hosts, ports, creds, channel_id="101", timeout=5,
                    workers=10, stop_on_first=True, on_host_done=None):
    """Scan up to `workers` hosts in parallel (capped at min(workers, #hosts)).
    Each host is handled by one thread that, sequentially: probes its ports for
    RTSP and, on every RTSP port, verifies `creds` **one login/password at a
    time** (never concurrent, so a single DVR isn't hammered — that risks a
    failed-login lockout). Parallelism is strictly across *different* hosts.

    Returns a flat list of hit dicts in input-host order:
      {"host", "port", "detail", "working": [(user, password, detail), ...]}
    `on_host_done(done_count, total, hits)` fires as each host finishes (progress).
    """
    def _process(host):
        hits = []
        for port in ports:
            ok, detail = probe_rtsp(host, port, timeout)
            if not ok:
                continue
            working = (probe_credentials(host, port, creds, channel_id, timeout,
                                         stop_on_first=stop_on_first) if creds else [])
            hits.append({"host": host, "port": port, "detail": detail, "working": working})
        return hits

    total = len(hosts)
    n = min(workers, total) or 1
    per_host = [None] * total
    with ThreadPoolExecutor(max_workers=n) as ex:
        futs = {ex.submit(_process, h): i for i, h in enumerate(hosts)}
        done = 0
        for fut in as_completed(futs):
            i = futs[fut]
            per_host[i] = fut.result()
            done += 1
            if on_host_done:
                on_host_done(done, total, per_host[i])
    return [hit for host_hits in per_host if host_hits for hit in host_hits]


def device_entry(host, rtsp_port, user, password, http_port="80", name=None):
    """Shape a verified scan hit into an import-ready device dict (the same shape
    the app stores). The HTTP/ISAPI `port` (snapshots + motion) can't be found by
    an RTSP scan, so it defaults to 80 — adjust after import if the DVR's web port
    differs. Written by `rtsp-scan --output`, consumed by `import`."""
    return {"name": name or f"cam {host}", "host": host, "port": str(http_port),
            "user": user, "password": password, "rtsp_port": str(rtsp_port)}


def probe_credentials(host, port, creds, channel_id="101", timeout=5,
                      stop_on_first=True, on_attempt=None):
    """Try each (user, password) in `creds` against a known-RTSP host:port, one
    at a time in the given order (login 1 with every password, then login 2, ...).
    Sequential on purpose: many DVRs lock the account/IP after a burst of failed
    logins, so we don't hammer with concurrent attempts. By default `stop_on_first`
    returns as soon as one credential is accepted (a port needs only one working
    login) — set it False to collect every accepted credential.

    `on_attempt(index, total, user, password)` (1-based index) is called just
    before each attempt, so a caller can render progress. Returns the accepted
    ones as (user, password, detail)."""
    working = []
    total = len(creds)
    for i, (user, password) in enumerate(creds, 1):
        if on_attempt:
            on_attempt(i, total, user, password)
        code, detail = rtsp_describe(host, port, user, password, channel_id, timeout)
        if code == 200:
            working.append((user, password, detail))
            if stop_on_first:
                break
    return working
