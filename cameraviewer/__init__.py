"""Camera Viewer — a zero-dependency local GUI for Hikvision-style DVR/NVRs.

Package layout:
    config   device store + persistence (~/.camera_viewer.json)
    camera   ISAPI HTTP access (Digest/Basic) + channel discovery
    motion   motion-detection grid codec + read/modify/write
    web      loads the static frontend (static/index.html, static/app.js)
    server   HTTP request handler + server + run_gui()
    cli      argparse entrypoint (GUI + `discover` subcommand)

Only the Python standard library is used — no third-party dependencies.
"""

__all__ = ["config", "camera", "motion", "web", "server", "cli"]
