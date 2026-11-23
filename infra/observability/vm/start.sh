#!/usr/bin/env bash
# Start (or restart) the observability stack natively on a host without Docker (the team VM).
# Install the binaries first with install.sh. Idempotent.
#
#   infra/observability/vm/start.sh          # start / restart everything
#   infra/observability/vm/start.sh stop     # stop everything
#
# Each component gets its own loopback address in /etc/hosts, named like the Docker Compose
# service, so the config files in infra/observability work unchanged here and in Docker.
# Grafana listens on grafana:3001; nginx publishes it at https://<host>:3501/grafana/.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/../../.." && pwd)
CFG=$REPO/infra/observability
ROOT=${OBS_HOME:-$HOME/observability}
BIN=$ROOT/bin
DATA=$ROOT/data
LOGS=$ROOT/logs
GRAFANA_ROOT_URL=${GRAFANA_ROOT_URL:-https://10.1.75.79:3501/grafana/}

declare -A HOSTS=(
  [otel-collector]=127.0.0.21 [prometheus]=127.0.0.22 [loki]=127.0.0.23 [tempo]=127.0.0.24
  [grafana]=127.0.0.25 [node-exporter]=127.0.0.26 [postgres-exporter]=127.0.0.27
  [redis-exporter]=127.0.0.28 [gateway]=127.0.0.1
)

stop_all() {
  for p in otelcol-contrib prometheus loki tempo grafana node_exporter postgres_exporter \
           redis_exporter; do
    pkill -f "$BIN/$p " 2>/dev/null || true
  done
  pkill -f "cgroup-stats.sh" 2>/dev/null || true
  pkill -f "$ROOT/grafana/bin/grafana server" 2>/dev/null || true
  # Wait for them to exit (Tempo and Loki flush on shutdown), so the ports are free again
  for _ in $(seq 30); do
    pgrep -f "$BIN/|$ROOT/grafana/bin/grafana server" >/dev/null || return 0
    sleep 1
  done
  pkill -9 -f "$BIN/|$ROOT/grafana/bin/grafana server" 2>/dev/null || true
  sleep 1
}

if [ "${1:-}" = stop ]; then stop_all; echo stopped; exit 0; fi

# --- one-time setup ------------------------------------------------------------------------
for name in "${!HOSTS[@]}"; do
  grep -qE "^${HOSTS[$name]}[[:space:]]+$name\$" /etc/hosts \
    || echo "${HOSTS[$name]} $name" | sudo tee -a /etc/hosts >/dev/null
done
ip() { echo "${HOSTS[$1]}"; }

mkdir -p "$DATA"/{prometheus,loki,tempo,grafana} "$LOGS"

# RabbitMQ serves its own metrics on :15692 once the plugin is on
if ! sudo rabbitmq-plugins list -e 2>/dev/null | grep -q rabbitmq_prometheus; then
  sudo rabbitmq-plugins enable rabbitmq_prometheus >/dev/null
fi

# Read-only monitoring role for postgres_exporter
if [ ! -s "$ROOT/.pg-monitor" ]; then
  pass=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)
  sudo -u postgres psql -qc "DO \$\$BEGIN
      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='farm_monitor') THEN
        CREATE ROLE farm_monitor LOGIN; END IF; END\$\$;
    ALTER ROLE farm_monitor PASSWORD '$pass'; GRANT pg_monitor TO farm_monitor;"
  echo "$pass" > "$ROOT/.pg-monitor" && chmod 600 "$ROOT/.pg-monitor"
fi
if [ ! -s "$ROOT/.grafana-admin" ]; then
  head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20 > "$ROOT/.grafana-admin"
  chmod 600 "$ROOT/.grafana-admin"
fi

# --- start ---------------------------------------------------------------------------------
stop_all
run() { # name, command...
  local name=$1; shift
  nohup "$@" > "$LOGS/$name.log" 2>&1 &
}

run cgroup-stats "$CFG/vm/cgroup-stats.sh" "$DATA/textfile"
run node-exporter "$BIN/node_exporter" --web.listen-address="$(ip node-exporter):9100" \
  --collector.textfile.directory="$DATA/textfile" \
  --no-collector.hwmon --no-collector.thermal_zone --no-collector.powersupplyclass

DATA_SOURCE_NAME="postgresql://farm_monitor:$(cat "$ROOT/.pg-monitor")@127.0.0.1:5432/postgres?sslmode=disable" \
  run postgres-exporter "$BIN/postgres_exporter" \
  --web.listen-address="$(ip postgres-exporter):9187"

run redis-exporter "$BIN/redis_exporter" -redis.addr=redis://127.0.0.1:6379 \
  -web.listen-address="$(ip redis-exporter):9121"

LOKI_LISTEN=$(ip loki) LOKI_DATA=$DATA/loki \
  run loki "$BIN/loki" -config.file="$CFG/loki.yaml" -config.expand-env=true

TEMPO_LISTEN=$(ip tempo) TEMPO_DATA=$DATA/tempo \
  PROM_REMOTE_WRITE="http://prometheus:9090/api/v1/write" \
  run tempo "$BIN/tempo" -config.file="$CFG/tempo.yaml" -config.expand-env=true \
  -live-store.shutdown_marker_dir="$DATA/tempo/live-store/shutdown-marker" \
  -live-store.wal.path="$DATA/tempo/live-store/traces" \
  -block-builder.wal.path="$DATA/tempo/block-builder/traces" \
  -backend-schedulerbackend-scheduler.local-work-path="$DATA/tempo/scheduler"

run prometheus "$BIN/prometheus" --config.file="$CFG/prometheus.yml" \
  --storage.tsdb.path="$DATA/prometheus" --storage.tsdb.retention.time=15d \
  --storage.tsdb.retention.size=8GB --web.listen-address="$(ip prometheus):9090" \
  --web.enable-remote-write-receiver --enable-feature=exemplar-storage

OTELCOL_LISTEN=$(ip otel-collector) FARM_LOG_GLOB="$HOME/logs/*.log" \
  FARM_ENVIRONMENT=staging TEMPO_OTLP=tempo:4317 LOKI_OTLP=http://loki:3100/otlp \
  run otel-collector "$BIN/otelcol-contrib" --config="$CFG/otel-collector.yaml"

GF_PATHS_DATA=$DATA/grafana GF_PATHS_LOGS=$LOGS GF_PATHS_PLUGINS=$DATA/grafana/plugins \
  GF_PATHS_PROVISIONING=$CFG/grafana/provisioning \
  GF_SERVER_HTTP_ADDR=$(ip grafana) GF_SERVER_HTTP_PORT=3001 \
  GF_SERVER_ROOT_URL=$GRAFANA_ROOT_URL GF_SERVER_SERVE_FROM_SUB_PATH=true \
  GF_SECURITY_ADMIN_USER=admin GF_SECURITY_ADMIN_PASSWORD="$(cat "$ROOT/.grafana-admin")" \
  GF_ANALYTICS_REPORTING_ENABLED=false GF_ANALYTICS_CHECK_FOR_UPDATES=false \
  GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false GF_NEWS_NEWS_FEED_ENABLED=false \
  GF_USERS_DEFAULT_THEME=dark GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=$CFG/grafana/dashboards/farm-overview.json \
  PROMETHEUS_URL=http://prometheus:9090 LOKI_URL=http://loki:3100 TEMPO_URL=http://tempo:3200 \
  FARM_DASHBOARDS=$CFG/grafana/dashboards \
  run grafana "$ROOT/grafana/bin/grafana" server --homepath "$ROOT/grafana"

sleep 8
check() { printf "%-18s %s\n" "$1" "$(curl -s -m 3 -o /dev/null -w '%{http_code}' "$2")"; }
check otel-collector http://otel-collector:13133/
check prometheus     http://prometheus:9090/-/ready
check loki           http://loki:3100/ready
check tempo          http://tempo:3200/ready
check grafana        http://grafana:3001/api/health
check node           http://node-exporter:9100/metrics
check postgres       http://postgres-exporter:9187/metrics
check redis          http://redis-exporter:9121/metrics
check rabbitmq       http://rabbitmq:15692/metrics
echo "Grafana: $GRAFANA_ROOT_URL  (admin / password in $ROOT/.grafana-admin)"
