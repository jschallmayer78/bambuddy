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

ARG BAMBUDDY_REPO=https://github.com/jschallmayer78/bambuddy.git
ARG BAMBUDDY_REF=feature/ha-addon
# What the cache is keyed on. A `git fetch` of a *branch* is the same shell
# command whatever the branch now points at, so Docker reuses the layer it
# built last time and the add-on installs an update that contains none of the
# new commits — the image is byte-identical to the one it replaced, while the
# Supervisor happily reports the new version. That trap cost a debugging round:
# a camera fix sat pushed and installed for an evening without ever running.
#
# So the source is pinned by content, not by name. Fetching this URL is the one
# instruction Docker re-runs on every build (it re-downloads and hashes the
# response), and its body changes exactly when the branch head moves — which
# breaks the cache for everything below it and nothing else. It must name the
# same repository and ref as the two ARGs above.
#
# It is github.com's own commit feed rather than api.github.com on purpose: the
# clone below already needs that host, so this adds no new reachability
# requirement and no API rate limit to a build that would otherwise fail hard —
# ADD cannot be made optional.
#
# Setting BAMBUDDY_REF to a full commit SHA pins the build outright and makes
# this fetch redundant but harmless: the SHA is part of the command below, so a
# changed SHA invalidates the cache on its own.
ARG BAMBUDDY_HEAD_URL=https://github.com/jschallmayer78/bambuddy/commits/feature/ha-addon.atom

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ADD ${BAMBUDDY_HEAD_URL} /tmp/bambuddy-head.atom

# `git fetch` rather than `git clone --branch`, because fetch takes a commit SHA
# as well as a branch or tag — so the same line serves both "track the branch"
# and "build exactly this commit". Cloning the default branch by name breaks
# whenever a repository renames it, which is how the SnapCon add-on's build
# broke once; leaving BAMBUDDY_REF empty follows the remote's own HEAD instead.
#
# The resolved commit is written into the image so the running add-on can say
# which source it was built from — see run.sh. "I pushed the fix and nothing
# changed" is otherwise unanswerable from the outside.
RUN git init -q /src \
    && git -C /src remote add origin "$BAMBUDDY_REPO" \
    && if [ -n "$BAMBUDDY_REF" ]; then \
           git -C /src fetch -q --depth 1 origin "$BAMBUDDY_REF"; \
       else \
           git -C /src fetch -q --depth 1 origin HEAD; \
       fi \
    && git -C /src checkout -q --detach FETCH_HEAD \
    && git -C /src rev-parse HEAD > /src/BUILD_COMMIT

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
COPY --from=frontend-builder /src/BUILD_COMMIT /app/BUILD_COMMIT

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
