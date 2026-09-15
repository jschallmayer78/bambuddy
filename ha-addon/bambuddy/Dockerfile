# Bambuddy, packaged as a Home Assistant add-on.
#
# The Supervisor builds this with the add-on folder as its context, so the
# application source cannot simply be COPYed in from the repository — it is
# cloned at BAMBUDDY_REF instead. Point that at a tag, a branch or a fork to
# run something other than this repository's default branch.
#
# The base image is set here rather than in a build.yaml: Supervisor 2026.04
# and later no longer read build.yaml or pass BUILD_FROM, and older ones passed
# their own Alpine base, which has no Node and no Python — an add-on that
# relied on it failed at the first `npm ci` with "npm: not found".
#
# Deliberately NOT carried over from the main image:
#   * Tailscale — Home Assistant has its own Tailscale add-on, and the CLI here
#     would have no daemon socket to talk to.
#   * setcap on the interpreter — an add-on on the host network already runs as
#     root, so binding 990/FTPS for the virtual printer needs no capability
#     juggling.
# ffmpeg IS carried over: the camera paths shell out to it for RTSP and for
# every snapshot taken from a stream.

FROM node:22-bookworm-slim AS frontend-builder

ARG BAMBUDDY_REPO=https://github.com/maziggy/bambuddy.git
ARG BAMBUDDY_REF=main

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Cloning the default branch by name breaks whenever a repository renames it,
# which is exactly how the SnapCon add-on's build broke once. `--branch` takes
# a tag or a branch; leaving BAMBUDDY_REF empty clones whatever the remote's
# HEAD points at.
RUN if [ -n "$BAMBUDDY_REF" ]; then \
        git clone --depth 1 --branch "$BAMBUDDY_REF" "$BAMBUDDY_REPO" /src; \
    else \
        git clone --depth 1 "$BAMBUDDY_REPO" /src; \
    fi

WORKDIR /src/frontend
RUN npm ci && npm run build


FROM python:3.13-slim-trixie

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ffmpeg \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The built frontend lands in /src/static, which is where the backend expects it.
COPY --from=frontend-builder /src/requirements.txt /app/requirements.txt
RUN pip install --root-user-action=ignore --upgrade 'pip>=26.1.2' \
    && pip install --root-user-action=ignore -r /app/requirements.txt

COPY --from=frontend-builder /src/backend /app/backend
COPY --from=frontend-builder /src/static /app/static
COPY --from=frontend-builder /src/slicer-api /app/slicer-api

COPY run.sh /run.sh
RUN chmod +x /run.sh

# Every writable thing Bambuddy owns goes to the add-on's own /data volume,
# which is what the Supervisor snapshots and restores.
ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib

# An MJPEG camera stream is a response that never completes, so uvicorn's
# default "wait forever for in-flight requests" would turn every add-on restart
# into a SIGKILL — no WAL checkpoint, no MQTT disconnect.
ENV UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN=5

CMD ["/run.sh"]
