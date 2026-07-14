#!/usr/bin/env bash
# One-command SIL run (Gate G2). Brings up the full stack incl. simulators,
# verifies they came up, starts the control loop, runs the seven scenarios,
# then tears down.
set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root (edge-ems/)

export INFLUX_TOKEN="${INFLUX_TOKEN:-change-me}"
# Activate the 'sil' profile for ALL compose calls; without it `docker compose
# ps` won't list the profiled simulators and the health gate sees 'missing'.
export COMPOSE_PROFILES="sil"
COMPOSE="deploy/docker-compose.yml"
SIMS=(sim-grid sim-bess sim-pv sim-load sim-meter)

teardown() { docker compose -f "$COMPOSE" --profile sil down; }

echo ">> building & starting stack (influx + mosquitto + core + controller + simulators)"
docker compose -f "$COMPOSE" --profile sil up -d --build

# --- health gate: every simulator must be running, not exited/restarting -----
echo ">> health-gate: verifying simulator containers"
sleep 3   # brief settle so an immediate crash shows as 'exited', not 'created'
failed=0
for svc in "${SIMS[@]}"; do
  cid="$(docker compose -f "$COMPOSE" --profile sil ps -q "$svc" 2>/dev/null || true)"
  status=""
  [ -n "$cid" ] && status="$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null || true)"
  if [ "$status" != "running" ]; then
    echo "   !! $svc not running (status='${status:-missing}')"
    [ -n "$cid" ] && docker logs --tail 25 "$cid" 2>&1 | sed 's/^/      | /' || true
    failed=1
  else
    echo "   ok $svc"
  fi
done
if [ "$failed" -ne 0 ]; then
  echo ">> HEALTH-GATE FAILED: a simulator did not come up. Tearing down."
  teardown
  exit 1
fi

echo ">> waiting for controller /health"
healthy=0
for i in $(seq 1 30); do
  if curl -sf http://localhost:5000/health >/dev/null; then healthy=1; break; fi
  sleep 2
done
if [ "$healthy" -ne 1 ]; then
  echo ">> controller never became healthy. Tearing down."
  teardown
  exit 1
fi

echo ">> running SIL scenarios"
set +e
EDGE_EMS_SIL=1 python -m pytest tests/sil/test_sil.py -v
rc=$?
set -e

echo ">> tearing down"
teardown
exit $rc
