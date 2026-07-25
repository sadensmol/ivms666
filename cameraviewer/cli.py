"""Command-line entrypoint: launch the GUI, or `discover` cameras from a terminal."""

import argparse

from . import camera
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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Hikvision-style camera viewer")
    sub = parser.add_subparsers(dest="cmd")
    d = sub.add_parser("discover", help="list available cameras and exit")
    d.add_argument("--host", required=True)
    d.add_argument("--port", default="80")
    d.add_argument("--user", required=True)
    d.add_argument("--password", required=True)
    ns = parser.parse_args(argv)
    if ns.cmd == "discover":
        run_discover(ns)
    else:
        run_gui()
