#!/usr/bin/env bash
# Container entrypoint: start IB Gateway via the base image's run.sh, wait for the API
# port, migrate the database, then run the service. SIGTERM stops both.
set -Eeuo pipefail

: "${TRADING_MODE:=paper}"
: "${IB_PORT:=4002}"
: "${DATABASE_URL:?DATABASE_URL is required}"
: "${HMAC_BOT_SECRET:?HMAC_BOT_SECRET is required}"
: "${HMAC_ADMIN_SECRET:?HMAC_ADMIN_SECRET is required}"

if [[ "$TRADING_MODE" == "live" && "${CONFIRM_LIVE:-}" != "yes" ]]; then
  echo "refusing to start: TRADING_MODE=live without CONFIRM_LIVE=yes" >&2
  exit 3
fi
if [[ "$TRADING_MODE" == "paper" && "$IB_PORT" != "4002" ]] || \
   [[ "$TRADING_MODE" == "live" && "$IB_PORT" != "4001" ]]; then
  echo "refusing to start: TRADING_MODE=$TRADING_MODE does not match IB_PORT=$IB_PORT" >&2
  exit 3
fi

cd /app

GATEWAY_PID=""
SERVICE_PID=""
shutdown() {
  echo ".> stopping service and gateway"
  [[ -n "$SERVICE_PID" ]] && kill -TERM "$SERVICE_PID" 2>/dev/null || true
  [[ -n "$GATEWAY_PID" ]] && kill -TERM "$GATEWAY_PID" 2>/dev/null || true
  wait "$SERVICE_PID" "$GATEWAY_PID" 2>/dev/null || true
}
trap shutdown SIGINT SIGTERM

echo ".> starting IB Gateway (${TRADING_MODE}) via run.sh"
"${HOME}/scripts/run.sh" &
GATEWAY_PID=$!

echo ".> waiting for the gateway API on 127.0.0.1:${IB_PORT} (IB Key approval may be needed)"
for _ in $(seq 1 180); do           # up to 15 minutes
  if nc -z 127.0.0.1 "$IB_PORT" 2>/dev/null; then break; fi
  if ! kill -0 "$GATEWAY_PID" 2>/dev/null; then
    echo "gateway process exited before the API came up" >&2
    exit 4
  fi
  sleep 5
done
if ! nc -z 127.0.0.1 "$IB_PORT" 2>/dev/null; then
  echo "gateway API never came up on ${IB_PORT}; starting the service anyway so it can alert" >&2
fi

# The base image relays the API port to 0.0.0.0:4003/4004 via socat for external
# clients. Nothing outside this container may reach the gateway, so stop the relay.
pkill -f run_socat.sh 2>/dev/null || true
pkill -x socat 2>/dev/null || true

echo ".> running database migrations"
/opt/venv/bin/alembic upgrade head

echo ".> starting execution service"
/opt/venv/bin/python -m ibkr_exec.main &
SERVICE_PID=$!

# Exit when either process exits, so Render restarts the whole container.
wait -n "$GATEWAY_PID" "$SERVICE_PID"
status=$?
echo ".> a process exited with status ${status}; shutting down"
shutdown
exit "$status"
