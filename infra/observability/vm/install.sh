#!/usr/bin/env bash
# Install the observability stack as native binaries (for hosts that cannot run Docker, like the
# team VM). Idempotent: skips what is already installed. Verifies checksums where the project
# publishes them.
#
#   infra/observability/vm/install.sh            # installs into ~/observability
#
# Then start everything with infra/observability/vm/start.sh.
set -euo pipefail

PROMETHEUS=3.16.0
LOKI=3.7.10
TEMPO=3.1.1
GRAFANA=13.3.1
OTELCOL=0.165.0
NODE_EXPORTER=1.13.1
POSTGRES_EXPORTER=0.21.1
REDIS_EXPORTER=1.98.0
K6=2.4.0

ROOT=${OBS_HOME:-$HOME/observability}
BIN=$ROOT/bin
DL=$ROOT/downloads
mkdir -p "$BIN" "$DL"
cd "$DL"

fetch_one() { # url ; checks url.sha256 (a file with just the hash, or "hash  name")
  local url=$1 file
  file=$(basename "$url")
  [ -s "$file" ] || curl -fsSL --retry 8 --retry-all-errors -C - -o "$file" "$url"
  echo "$(curl -fsSL --retry 3 "$url.sha256" | awk '{print $1}')  $file" | sha256sum -c - >/dev/null \
    || { echo "checksum mismatch: $file" >&2; rm -f "$file"; exit 1; }
  echo "verified $file"
}

fetch() { # url [sha256-list-url]
  local url=$1 sums=${2:-} file
  file=$(basename "$url")
  [ -s "$file" ] || curl -fsSL --retry 8 --retry-all-errors -C - -o "$file" "$url"
  if [ -n "$sums" ]; then
    curl -fsSL --retry 3 "$sums" | grep " \*\?$file\$" | sed 's/\*//' | sha256sum -c - >/dev/null \
      || { echo "checksum mismatch: $file" >&2; rm -f "$file"; exit 1; }
    echo "verified $file"
  fi
}

gh=https://github.com

if [ ! -x "$BIN/prometheus" ]; then
  f=prometheus-$PROMETHEUS.linux-amd64
  fetch $gh/prometheus/prometheus/releases/download/v$PROMETHEUS/$f.tar.gz \
        $gh/prometheus/prometheus/releases/download/v$PROMETHEUS/sha256sums.txt
  tar xzf $f.tar.gz && cp $f/prometheus $f/promtool "$BIN/"
fi

if [ ! -x "$BIN/node_exporter" ]; then
  f=node_exporter-$NODE_EXPORTER.linux-amd64
  fetch $gh/prometheus/node_exporter/releases/download/v$NODE_EXPORTER/$f.tar.gz \
        $gh/prometheus/node_exporter/releases/download/v$NODE_EXPORTER/sha256sums.txt
  tar xzf $f.tar.gz && cp $f/node_exporter "$BIN/"
fi

if [ ! -x "$BIN/postgres_exporter" ]; then
  f=postgres_exporter-$POSTGRES_EXPORTER.linux-amd64
  fetch $gh/prometheus-community/postgres_exporter/releases/download/v$POSTGRES_EXPORTER/$f.tar.gz \
        $gh/prometheus-community/postgres_exporter/releases/download/v$POSTGRES_EXPORTER/sha256sums.txt
  tar xzf $f.tar.gz && cp $f/postgres_exporter "$BIN/"
fi

if [ ! -x "$BIN/redis_exporter" ]; then
  f=redis_exporter-v$REDIS_EXPORTER.linux-amd64
  fetch $gh/oliver006/redis_exporter/releases/download/v$REDIS_EXPORTER/$f.tar.gz \
        $gh/oliver006/redis_exporter/releases/download/v$REDIS_EXPORTER/sha256sums.txt
  tar xzf $f.tar.gz && cp $f/redis_exporter "$BIN/"
fi

if [ ! -x "$BIN/loki" ]; then
  fetch $gh/grafana/loki/releases/download/v$LOKI/loki-linux-amd64.zip \
        $gh/grafana/loki/releases/download/v$LOKI/SHA256SUMS
  python3 -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall('.')" loki-linux-amd64.zip
  install -m 755 loki-linux-amd64 "$BIN/loki"
fi

if [ ! -x "$BIN/tempo" ]; then
  f=tempo_${TEMPO}_linux_amd64.tar.gz
  fetch $gh/grafana/tempo/releases/download/v$TEMPO/$f \
        $gh/grafana/tempo/releases/download/v$TEMPO/SHA256SUMS
  mkdir -p tempo && tar xzf $f -C tempo && cp tempo/tempo "$BIN/"
fi

if [ ! -x "$BIN/otelcol-contrib" ]; then
  f=otelcol-contrib_${OTELCOL}_linux_amd64.tar.gz
  fetch_one $gh/open-telemetry/opentelemetry-collector-releases/releases/download/v$OTELCOL/$f
  mkdir -p otelcol && tar xzf $f -C otelcol && cp otelcol/otelcol-contrib "$BIN/"
fi

if [ ! -x "$BIN/k6" ]; then
  f=k6-v$K6-linux-amd64
  fetch $gh/grafana/k6/releases/download/v$K6/$f.tar.gz \
        $gh/grafana/k6/releases/download/v$K6/k6-v$K6-checksums.txt
  tar xzf $f.tar.gz && cp $f/k6 "$BIN/"
fi

if [ ! -d "$ROOT/grafana" ]; then
  f=grafana-$GRAFANA.linux-amd64.tar.gz
  fetch_one https://dl.grafana.com/oss/release/$f
  mkdir -p grafana-src && tar xzf $f -C grafana-src
  mv grafana-src/grafana-* "$ROOT/grafana"
fi

echo "installed into $BIN:"
ls "$BIN"
