#!/usr/bin/env bash
# Run a k6 load test against the team VM and stream its results into the VM's Prometheus, so
# the Grafana dashboard "Farm · Load test & capacity" shows k6 next to the servers.
#
#   ADMIN_PHONE=+91... ADMIN_PASSWORD=... loadtest/run.sh smoke|load|stress|spike|soak|auth
#
# Optional: BASE_URL (default the VM gateway), STAGES="1m:100,3m:100,1m:0" (custom ramp),
# VM_SSH="-p 2501 student@10.1.75.79", K6=/path/to/k6. Results: loadtest/results/<testid>.*
# Test accounts all have +917000 phones; delete them afterwards with loadtest/cleanup.py.
set -euo pipefail
cd "$(dirname "$0")"

PROFILE=${1:-smoke}
VM_SSH=${VM_SSH:--p 2501 student@10.1.75.79}
export BASE_URL=${BASE_URL:-http://10.1.75.79:4501/api/v1}
: "${ADMIN_PHONE:?set ADMIN_PHONE}" "${ADMIN_PASSWORD:?set ADMIN_PASSWORD}"
K6=${K6:-k6}
TESTID=${TESTID:-$PROFILE-$(date +%Y%m%d-%H%M)}
mkdir -p results

# Tunnel to Prometheus on the VM (it only listens on the VM's loopback)
PORT=19090
ssh -o ExitOnForwardFailure=yes -fN -L $PORT:prometheus:9090 $VM_SSH -o ControlMaster=no
TUNNEL=$(pgrep -nf "ssh .*-L $PORT:prometheus:9090")
trap 'kill $TUNNEL 2>/dev/null || true' EXIT

export PROFILE K6_PROMETHEUS_RW_SERVER_URL=http://127.0.0.1:$PORT/api/v1/write
export K6_PROMETHEUS_RW_TREND_STATS="p(50),p(90),p(95),p(99),avg,max"
export K6_PROMETHEUS_RW_PUSH_INTERVAL=5s
echo "test id: $TESTID  (Grafana: Farm · Load test & capacity, Test run = $TESTID)"
"$K6" run --tag testid="$TESTID" -o experimental-prometheus-rw \
  --summary-export "results/$TESTID.summary.json" k6/farm.js 2>&1 | tee "results/$TESTID.log"
