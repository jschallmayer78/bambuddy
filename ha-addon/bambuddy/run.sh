#!/usr/bin/env bash
# Add-on entrypoint: turn the user's options into Bambuddy's environment, then
# hand over to uvicorn.
#
# `exec` is load-bearing. Without it this shell stays PID 1, uvicorn runs as
# its child and never sees the SIGTERM the Supervisor sends on stop — so every
# restart runs out the grace period and dies on SIGKILL, with no WAL
# checkpoint and no MQTT disconnect.
set -euo pipefail

OPTIONS=/data/options.json

read_option() {
    # bashio is not available in this base image (it ships with the Alpine
    # add-on bases), and pulling it in for four values is not worth it.
    python3 -c "
import json, sys
try:
    with open('${OPTIONS}') as handle:
        print(json.load(handle).get(sys.argv[1], sys.argv[2]))
except FileNotFoundError:
    print(sys.argv[2])
" "$1" "$2"
}

HA_AUTH="$(read_option ha_auth true)"
HA_AUTH_ROLE="$(read_option ha_auth_role user)"
PORT="$(read_option port 8000)"
LOG_LEVEL="$(read_option log_level info)"

case "$(printf '%s' "${HA_AUTH}" | tr '[:upper:]' '[:lower:]')" in
    true|1|yes|on) export BAMBUDDY_HA_INGRESS_AUTH=1 ;;
    *) export BAMBUDDY_HA_INGRESS_AUTH=0 ;;
esac
export BAMBUDDY_HA_INGRESS_ROLE="${HA_AUTH_ROLE}"

export DATA_DIR=/data
export PORT
# Bambuddy reads LOG_LEVEL uppercase; the add-on option is lowercase because
# that is what every other Home Assistant add-on offers. "trace" and "fatal"
# exist in the Supervisor's vocabulary but not Python's, so they are folded
# onto the nearest level that does.
case "${LOG_LEVEL}" in
    trace) LOG_LEVEL=DEBUG ;;
    fatal) LOG_LEVEL=CRITICAL ;;
    *) LOG_LEVEL="$(printf '%s' "${LOG_LEVEL}" | tr '[:lower:]' '[:upper:]')" ;;
esac
export LOG_LEVEL
export HOST=0.0.0.0

mkdir -p /data/archive /data/logs

echo "Bambuddy add-on starting on port ${PORT} (Home Assistant sign-in: ${BAMBUDDY_HA_INGRESS_AUTH}, role: ${HA_AUTH_ROLE})"

cd /app
exec uvicorn backend.app.main:app \
    --host "${HOST}" \
    --port "${PORT}" \
    --loop asyncio \
    --timeout-graceful-shutdown "${UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN:-5}"
