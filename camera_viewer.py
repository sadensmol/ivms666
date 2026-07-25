#!/usr/bin/env python3
"""
Camera Viewer — a zero-dependency local GUI for Hikvision-style DVR/NVRs
(Server: DNVRS-Webs, ISAPI + Digest auth).

This is a thin launcher; the implementation lives in the `cameraviewer` package
(config / camera / motion / web / server / cli). Only the Python standard
library is used — no pip/brew installs needed.

Run the GUI:
    python3 camera_viewer.py

List channels from the terminal:
    python3 camera_viewer.py discover --host <camera-ip> --port 80 \\
        --user admin --password 'secret'

Features: multiple cameras/NVRs with saved credentials, auto channel discovery,
a live snapshot gallery, per-tile remove, an HD live view, and a motion-detection
area editor that writes back to the device without disturbing other settings.
"""

from cameraviewer.cli import main

if __name__ == "__main__":
    main()
