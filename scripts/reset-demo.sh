#!/usr/bin/env bash
# Wipe every database, queue and photo, then rebuild the demo story from scratch (docker compose stack).
#   scripts/reset-demo.sh            reset and seed the curated demo
#   scripts/reset-demo.sh --build    also rebuild the service images first
# Admin login for the demo: +919999999999 / DemoAdmin@2026 (override with ADMIN_PHONE / ADMIN_PASSWORD).
set -euo pipefail
cd "$(dirname "$0")/.."
ADMIN_PHONE="${ADMIN_PHONE:-+919999999999}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-DemoAdmin@2026}"
BASE_URL="${SEED_BASE_URL:-http://localhost/api/v1}"

docker compose down -v
docker compose up -d ${1:+"$1"}

echo "Waiting for the services to answer..."
for service in identity catalog land media quality order logistics notification; do
  for _ in $(seq 1 90); do
    if curl -fs -o /dev/null "$BASE_URL/$service/health"; then break; fi
    sleep 2
  done
  curl -fs -o /dev/null "$BASE_URL/$service/health" || { echo "$service did not become healthy" >&2; exit 1; }
done

docker compose exec -T identity python -m identity_service.cli create-admin \
  --name "Platform Admin" --phone "$ADMIN_PHONE" --password "$ADMIN_PASSWORD"

SEED_ADMIN_PHONE="$ADMIN_PHONE" SEED_ADMIN_PASSWORD="$ADMIN_PASSWORD" \
  uv run --package farm-seed python -m farm_seed --base-url "$BASE_URL"
