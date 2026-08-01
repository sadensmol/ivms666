"""Command-line entrypoint: launch the GUI, or `discover` cameras from a terminal."""

import argparse
import shutil
import sys
import threading

from . import camera, config, scan
from .server import run_gui


def run_discover(ns):
    cfg = {"host": ns.host, "port": str(ns.port), "user": ns.user, "password": ns.password}
    channels = camera.discover_channels(cfg)
    if not channels:
        print("No cameras found (check host/port/credentials).")
        return
    print(f"Found {len(channels)} camera(s):")
    for ch in channels:
        print(f"  id={ch['id']:<6} input={ch['input']:<3} {ch['name']}")


def _csv(s):
    return [x.strip() for x in s.split(",") if x.strip()]


# Same-line progress: rewrite the current line with '\r' so it updates in place.
# _clear_progress wipes it before a permanent line (a found RTSP / a result) is
# printed on its own line, then the progress line re-renders on the next update.
_PROGRESS_WIDTH = 110


def _progress(msg):
    sys.stdout.write("\r" + msg[:_PROGRESS_WIDTH].ljust(_PROGRESS_WIDTH))
    sys.stdout.flush()


def _clear_progress():
    sys.stdout.write("\r" + " " * _PROGRESS_WIDTH + "\r")
    sys.stdout.flush()


class _LiveRegion:
    """A bottom-anchored, multi-line status region that redraws in place (ANSI).
    Permanent lines scroll ABOVE it via print_above(); the live block below shows
    one line per host currently being verified plus a host-progress line. On a
    non-tty (piped/redirected) all cursor control is skipped so output stays a
    clean sequence of permanent lines.

    Two hard rules keep the block from ever leaking into scrollback (the flood of
    repeated 'trying …' lines): every line is truncated to the terminal WIDTH so
    it never wraps onto a second physical row, and the block is capped to the
    terminal HEIGHT (surplus hosts collapse into one '+N more' line). Both matter
    because `render`'s `\\033[nA` cursor-up cannot climb above the top of the
    screen — if the block is taller than the viewport (or a line wraps), the move
    clamps and the rows that already scrolled off are never cleared, so each
    redraw stacks a fresh copy below the stale one."""

    def __init__(self):
        self.tty = sys.stdout.isatty()
        self.n = 0  # physical live lines currently drawn (== the last render's count)

    def _fit(self, lines):
        cols, rows = shutil.get_terminal_size((100, 24))
        cap = max(1, rows - 1)                        # leave a row so cursor-up never clamps
        if len(lines) > cap:                          # collapse the overflow into one line
            lines = lines[: cap - 1] + [f"      … +{len(lines) - cap + 1} more verifying"]
        return [line[: max(1, cols - 1)] for line in lines]   # truncate -> never wrap

    def render(self, lines):
        if not self.tty:
            return
        lines = self._fit(lines)
        out = f"\033[{self.n}A" if self.n else ""   # up to the region's top line
        out += "\033[0J"                            # clear from cursor to end of screen
        out += "".join(line + "\n" for line in lines)  # redraw the block
        sys.stdout.write(out)
        sys.stdout.flush()
        self.n = len(lines)

    def print_above(self, text):
        if self.tty and self.n:
            sys.stdout.write(f"\033[{self.n}A\033[0J")  # erase the live block first
            self.n = 0
        print(text)

    def clear(self):
        if self.tty and self.n:
            sys.stdout.write(f"\033[{self.n}A\033[0J")
            sys.stdout.flush()
        self.n = 0


def run_rtsp_scan(ns):
    scan_cfg = config.default_scan()  # range/ports/credentials — all lists of strings
    # Ports: --ports (comma-separated) overrides the config list; else scan.ports.
    ports = _csv(ns.ports) if ns.ports else list(scan_cfg["ports"])
    # Credentials to verify: default to scan.logins/scan.passwords from
    # default_config.json, overridden by --logins/--passwords when given.
    # Every login is tried with every password on each found port.
    logins = _csv(ns.logins) if ns.logins else list(scan_cfg["logins"])
    passwords = _csv(ns.passwords) if ns.passwords else list(scan_cfg["passwords"])
    if ns.user:
        logins.append(ns.user)
    if ns.password:
        passwords.append(ns.password)
    creds = scan.credential_combos(logins, passwords)

    if ns.host:
        hosts, target = [ns.host], ns.host
    else:
        # --range (one spec or comma-separated) overrides the config list; each
        # spec is expanded and the hosts are merged (de-duplicated).
        specs = _csv(ns.range) if ns.range else list(scan_cfg["range"])
        if not specs:
            print(f"No IP range to scan. Pass --range/--host, or set "
                  f'"scan": {{"range": ["..."]}} in {config.DEFAULTS_PATH}')
            return 2
        try:
            hosts = scan.expand_ranges(specs)
        except ValueError as e:
            print(f"bad range: {e}")
            return 2
        target = ", ".join(specs)
    # --- Windowed scan: a window of `workers` IPs is scanned port-by-port (each
    # port probed across all IPs in the window at once), then the next window of
    # IPs. Credentials are verified the instant a port answers. All output is
    # serialized through `lock` so live lines don't interleave. ---
    workers = min(ns.parallel, len(hosts)) or 1
    print(f"Scanning {len(hosts)} host(s) x {len(ports)} port(s) on {target} "
          f"({workers} IP(s) per window, one port at a time; credentials checked the instant a port is found) ...")
    lock = threading.Lock()
    region = _LiveRegion()
    active = {}  # (host, port) currently verifying -> its live "trying …" line
    stats = {"rtsp": 0, "ok": 0, "streams": 0, "hosts": 0, "total": len(hosts)}

    def _lines():
        # one live line per host being verified, then the host-search progress line
        return list(active.values()) + [
            f"    {stats['hosts']}/{stats['total']} hosts · "
            f"{stats['rtsp']} RTSP · {stats['ok']} verified · {stats['streams']} stream(s)"]

    def on_found(host, port, detail):
        with lock:
            stats["rtsp"] += 1
            region.print_above(f"  ✓ RTSP   {host}:{port}   {detail}")
            active[(host, port)] = f"      {host}:{port}  starting credential check…"
            region.render(_lines())

    def on_attempt(host, port, i, n, user, password):
        with lock:
            active[(host, port)] = f"      {host}:{port}  trying {user}:{password}  ({i}/{n})"
            region.render(_lines())

    def on_verified(hit):
        with lock:
            active.pop((hit["host"], hit["port"]), None)
            entries = []
            if hit["streams"]:
                stats["ok"] += 1
                stats["streams"] += len(hit["streams"])
                u, p, _, vendor = hit["streams"][0]
                region.print_above(f"  → login OK   {hit['host']}:{hit['port']}   {u}:{p}   "
                                   f"[{vendor}]   {len(hit['streams'])} stream(s):")
                for su, sp, path, _v in hit["streams"]:
                    region.print_above(f"       {scan.rtsp_link(hit['host'], hit['port'], path, user=su, password=sp)}")
                    entries.append(scan.device_entry(hit["host"], hit["port"], su, sp, path))
            elif hit.get("credential"):        # login worked, but no stream path matched
                u, p = hit["credential"]
                region.print_above(f"  → login OK but no stream path matched   "
                                   f"{hit['host']}:{hit['port']}   {u}:{p}   "
                                   f"(creds fine, URL schema unknown)")
            elif hit.get("reason") == "conn_dropped":
                lu, lp = hit.get("last") or (None, None)
                where = f" on {lu}:{lp}" if lu is not None else ""
                region.print_above(f"  → connection dropped   {hit['host']}:{hit['port']}   "
                                   f"(after {hit['attempts']}/{hit['total']} combo(s){where} — not all "
                                   f"tried; deprioritise that login next run)")
            elif creds:                        # every combo tried, each rejected
                tried = hit.get("attempts", len(creds))
                region.print_above(f"  → no valid login   {hit['host']}:{hit['port']}   "
                                   f"({tried}/{len(creds)} combo(s) tried)")
            else:
                entries.append(scan.device_entry(hit["host"], hit["port"]))   # no-creds -> anonymous default
            if entries:                                                        # append to the output file NOW
                config.merge_devices_file(ns.output, entries)
            region.render(_lines())

    def on_host_done(done, total, hits):
        with lock:
            stats["hosts"] = done
            region.render(_lines())

    found = scan.scan_and_verify(hosts, ports, creds, timeout=ns.timeout, workers=workers,
                                 on_found=on_found, on_attempt=on_attempt,
                                 on_verified=on_verified, on_host_done=on_host_done)
    region.clear()
    if not found:
        print(f"No RTSP found (ports tried: {', '.join(ports)}).")
        return 1

    # Each verified hit was already appended to the output file live (on_verified,
    # merged with whatever was there before — a re-run adds to it, never wipes it).
    # Print a compact final summary of the file's current contents (creds masked).
    merged = config.read_devices_file(ns.output)
    print(f"\n{stats['rtsp']} RTSP port(s) found · {stats['ok']} with a working login · "
          f"{stats['streams']} stream(s) enumerated.")
    print(f"{ns.output} now holds {len(merged)} stream(s) (new appended, existing kept):")
    for i, e in enumerate(merged, 1):
        print(f"  {i}. {config.strip_url_creds(e.get('rtsp_url', '') or e.get('host', ''))}")
    print(f"\nImport into the app:  python3 -m cameraviewer import --file {ns.output}")
    return 0


def run_import(ns):
    config.load()  # merge into whatever devices already exist
    try:
        entries = config.read_devices_file(ns.file)
    except (OSError, ValueError) as e:
        print(f"cannot read {ns.file}: {e}")
        return 2
    if not entries:
        print(f"No devices found in {ns.file}.")
        return 1
    added, skipped = config.import_devices(entries)
    tail = f" (skipped {skipped} duplicate/invalid)" if skipped else ""
    print(f"Imported {added} device(s){tail} into {config.CONFIG_PATH}.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Hikvision-style camera viewer")
    sub = parser.add_subparsers(dest="cmd")
    d = sub.add_parser("discover", help="list available cameras and exit")
    d.add_argument("--host", required=True)
    d.add_argument("--port", default="80")
    d.add_argument("--user", required=True)
    d.add_argument("--password", required=True)

    r = sub.add_parser("rtsp-scan", help="probe an IP (or range) for RTSP ports and print stream links")
    g = r.add_mutually_exclusive_group()  # neither -> use scan.range from config
    g.add_argument("--host", help="single IP/hostname to probe")
    g.add_argument("--range", help="IP range: CIDR '10.0.0.0/24', dash '10.0.0.5-9' / '10.0.0.5-10.0.0.9'")
    r.add_argument("--ports", default=None,
                   help="comma-separated ports to probe (default: from config / 554)")
    r.add_argument("--logins", default=None,
                   help="comma-separated logins to verify on each found port; every login "
                        "is tried with every password (overrides scan.logins from config)")
    r.add_argument("--passwords", default=None,
                   help="comma-separated passwords to verify (overrides scan.passwords from config)")
    r.add_argument("--user", help="extra single login to try (also used in the link)")
    r.add_argument("--password", help="extra single password to try")
    r.add_argument("--output", "-o", default="rtsp-scan-output.json",
                   help="JSON file to write verified devices to; feed it to "
                        "`import` to set up the app (default: rtsp-scan-output.json)")
    r.add_argument("--parallel", type=int, default=25,
                   help="window size — how many IPs to probe at once on each port "
                        "(one port at a time across the window; credentials still "
                        "tried one at a time per host) [25]")
    r.add_argument("--timeout", type=float, default=5.0)

    i = sub.add_parser("import", help="load devices from an rtsp-scan output file into the app")
    i.add_argument("--file", "-f", required=True, help="JSON file written by `rtsp-scan --output`")

    ns = parser.parse_args(argv)
    try:
        _dispatch(ns)
    except KeyboardInterrupt:
        # Ctrl+C during a scan: exit cleanly instead of dumping a thread-pool
        # traceback (in-flight probes finish on their own socket timeout).
        _clear_progress()
        print("\nInterrupted.")
        sys.exit(130)


def _dispatch(ns):
    if ns.cmd == "discover":
        run_discover(ns)
    elif ns.cmd == "rtsp-scan":
        sys.exit(run_rtsp_scan(ns))
    elif ns.cmd == "import":
        sys.exit(run_import(ns))
    else:
        run_gui()
