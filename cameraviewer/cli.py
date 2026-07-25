"""Command-line entrypoint: launch the GUI, or `discover` cameras from a terminal."""

import argparse
import sys

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


# Same-line scan progress: rewrite the current line with '\r' so it updates in
# place ("scanning 45 of 199 hosts  (3 RTSP found)"). Hosts run in parallel, so
# progress is per host (not per credential). _clear_progress wipes it before the
# results are printed on their own lines.
_PROGRESS_WIDTH = 72


def _scan_progress(done, total, rtsp):
    msg = f"    scanning {done} of {total} hosts  ({rtsp} RTSP found)"
    sys.stdout.write("\r" + msg[:_PROGRESS_WIDTH].ljust(_PROGRESS_WIDTH))
    sys.stdout.flush()


def _clear_progress():
    sys.stdout.write("\r" + " " * _PROGRESS_WIDTH + "\r")
    sys.stdout.flush()


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
    workers = min(ns.parallel, len(hosts)) or 1
    print(f"Scanning {len(hosts)} host(s) x {len(ports)} port(s) on {target} "
          f"({workers} in parallel) ...")
    # Probe + verify up to `workers` hosts concurrently; credentials are still
    # tried one at a time within each host. Progress is per host on one line.
    rtsp_seen = [0]

    def on_host_done(done, total, hits):
        rtsp_seen[0] += len(hits)
        _scan_progress(done, total, rtsp_seen[0])

    found = scan.scan_and_verify(hosts, ports, creds, timeout=ns.timeout,
                                 workers=workers, on_host_done=on_host_done)
    _clear_progress()
    if not found:
        print(f"No RTSP found (ports tried: {', '.join(ports)}).")
        return 1
    print(f"RTSP found ({len(found)}):")
    devices = []  # verified hits, shaped for the output file / `import`
    for hit in found:
        host, port, working = hit["host"], hit["port"], hit["working"]
        print(f"  {host}:{port}  {hit['detail']}")
        if not creds:
            # No credential lists -> just print an anonymous stream link (old behavior).
            print(f"    {scan.rtsp_link(host, port)}")
            continue
        if not working:
            print(f"    no working credentials ({len(creds)} combo(s) tried)")
            continue
        for user, password, status in working:
            link = scan.rtsp_link(host, port, user=user, password=password)
            print(f"    OK {user}:{password}  ({status})")
            print(f"       {link}")
        # One device per found port, using the first credential that worked.
        u, p, _ = working[0]
        devices.append(scan.device_entry(host, port, u, p))

    # Full summary to stdout: every field that goes into the output file, so the
    # console alone tells you exactly what was found and how to reach it.
    print(f"\nDevices found ({len(devices)}):")
    if not devices:
        print("  (none — no port had a working login/password)")
    for i, d in enumerate(devices, 1):
        print(f"  {i}. {d['name']}")
        print(f"       host:      {d['host']}")
        print(f"       http port: {d['port']}  (ISAPI/snapshots — default; edit if the web port differs)")
        print(f"       rtsp port: {d['rtsp_port']}")
        print(f"       login:     {d['user']}")
        print(f"       password:  {d['password']}")
        print(f"       rtsp url:  {scan.rtsp_link(d['host'], d['rtsp_port'], user=d['user'], password=d['password'])}")

    # Always emit the output file (independent of default_config.json). It is the
    # setup input for the app: `import --file <output>` loads it into the internal
    # config (~/.camera_viewer.json).
    config.write_devices_file(ns.output, devices)
    print(f"\nWrote {len(devices)} device(s) to {ns.output} "
          f"— set them up with:  python3 -m cameraviewer import --file {ns.output}")
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
    r.add_argument("--parallel", type=int, default=10,
                   help="how many hosts to probe/verify at once (capped at the host "
                        "count; credentials are still tried one at a time per host) [10]")
    r.add_argument("--timeout", type=float, default=5.0)

    i = sub.add_parser("import", help="load devices from an rtsp-scan output file into the app")
    i.add_argument("--file", "-f", required=True, help="JSON file written by `rtsp-scan --output`")

    ns = parser.parse_args(argv)
    if ns.cmd == "discover":
        run_discover(ns)
    elif ns.cmd == "rtsp-scan":
        sys.exit(run_rtsp_scan(ns))
    elif ns.cmd == "import":
        sys.exit(run_import(ns))
    else:
        run_gui()
