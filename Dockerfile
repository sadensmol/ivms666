# ivms666 container image.
#
# Stdlib-only Python (invariant #1) — there is no requirements.txt and there
# never will be. The single external binary is ffmpeg, used by live.py /
# playback.py / recordings.py for RTSP work.
FROM python:3.13-slim

# ffmpeg is the one allowed non-Python dependency (Live view, still grabs, clips).
# tzdata so a TZ= in compose actually resolves (event-log timestamps read better
# in local time; the DVR's own clock is untouched by this).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata \
 && rm -rf /var/lib/apt/lists/*

# HOME points at the mounted volume, so config.CONFIG_PATH (~/.ivms666.json,
# chmod 0600) and DEFAULT_SAVE_PATH (~/ivms666) persist across deploys with no
# code change. Listening on 0.0.0.0 is safe ONLY because the container publishes
# no host port — the cloudflared sidecar is the sole route in.
ENV HOME=/data \
    CV_LISTEN_HOST=0.0.0.0 \
    CV_LISTEN_PORT=8777 \
    CV_NO_BROWSER=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --uid 10001 --create-home --home-dir /data app
WORKDIR /app
# Flat layout: the modules, static/, vendors/ and default_config.json all live at
# the repo root. .dockerignore keeps tests/docs/secrets out.
COPY . .

USER app
EXPOSE 8777

# stdlib healthcheck — no curl in the image, and /devices needs no camera.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python3", "-c", \
         "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8777/devices', timeout=4)"]

CMD ["python3", "ivms666.py"]
