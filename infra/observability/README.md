# Observability: metrics, logs and traces

Grafana shows what every service is doing, on four dashboards in the **Farm Platform** folder:

| Dashboard | What it answers |
|---|---|
| **Farm · Service health** (home) | Requests/s, error rate, p50/p95/p99 latency per service and per route; gateway status codes and rate limiting |
| **Farm · Load test & capacity** | k6 virtual users, throughput and latency next to server CPU, database connections and business throughput (orders, logins, searches per minute) |
| **Farm · Infrastructure** | VM CPU/memory against the container's limits, PostgreSQL, Redis, RabbitMQ queues and dead letters, event handling, background jobs, the telemetry pipeline |
| **Farm · Logs & traces** | Log search by service, level and text; error logs; service map; slowest and failed traces; event flows |

Where it runs:

- **Team VM**: `https://10.1.75.79:3501/grafana/` (log in as `admin`; the password is in `~/observability/.grafana-admin` on the VM)
- **Local Docker**: `docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d`, then `http://localhost:3001` (admin / admin)

## How it fits together

```
services (FastAPI, workers) --OTLP--> OpenTelemetry Collector --> Tempo       (traces)
gateway (Traefik)          --OTLP-->        |                  --> Prometheus  (metrics, :8889)
service log files (JSON) --tail-->          |                  --> Loki        (logs, OTLP)
Prometheus scrapes: collector, Traefik, postgres/redis/node exporters, RabbitMQ
k6 and Tempo's metrics-generator push into Prometheus (remote write)
Grafana reads Prometheus, Loki and Tempo
```

- **Instrumentation** lives in `libs/common/src/farm_common/telemetry.py` and is on in a process
  when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (off in tests). Every service gets it through
  `create_app()`; the two arq workers call `setup_telemetry()` at startup.
- **Traces**: gateway span, then one span per request, per SQL statement and per outgoing HTTP
  call; publishing an event and each consumer handling it are spans in the same trace, even when
  the outbox relay publishes it seconds later (the correlation id is the trace id, and the outbox
  keeps it). Background jobs (`@traced_job`) get their own traces.
- **Logs**: the JSON log lines gain `traceId` / `spanId`. In Grafana, open a log line and click
  **Open trace**; in a trace, click a span's log icon to see that service's lines for the trace.
- **Metrics**: `http_server_request_duration_seconds` (route, method, status, service),
  `farm_events_published_total`, `farm_events_consumed_total{outcome}`,
  `farm_events_handler_duration_seconds`, `farm_jobs_duration_seconds`, `process_*` per service,
  plus the exporters. On the VM, `vm_cgroup_*` are the container's own CPU and memory use and
  limits (`node_exporter` alone would show the host's 120 CPUs).
- **Sampling**: `OTEL_TRACES_SAMPLER_ARG` (default 1.0 = every trace). Use e.g. 0.1 for long, heavy
  load tests; metrics are never sampled.

## Files

| File | |
|---|---|
| `otel-collector.yaml` | receivers (OTLP, log files), processors, exporters |
| `prometheus.yml` | scrape targets |
| `loki.yaml`, `tempo.yaml` | single-binary configs, files on disk (Tempo 3.x needs the extra path flags passed in `vm/start.sh` / compose) |
| `traefik.yml` | gateway config with Prometheus metrics (:8082) and OTLP tracing; used instead of `infra/traefik/traefik.yml` when observability is on |
| `grafana/provisioning/` | datasources (linked to each other) and the dashboard provider |
| `grafana/build_dashboards.py` | generates `grafana/dashboards/*.json`. Edit the script, then run it |
| `vm/install.sh` | downloads pinned versions of every binary into `~/observability` (checksums verified) |
| `vm/start.sh` | starts or restarts the stack on the VM (`stop` to stop it) |
| `vm/cgroup-stats.sh` | writes the container's CPU/memory use and limits for node_exporter |

## Team VM (no Docker)

The VM is an unprivileged container, so the stack runs as native binaries, like the backend.
Each component gets its own loopback address in `/etc/hosts` (`otel-collector` 127.0.0.21,
`prometheus` .22, `loki` .23, `tempo` .24, `grafana` .25, exporters .26-.28), so the same config
files work in Docker and on the VM. Nothing listens on a public port except through nginx
(`/grafana/` on 3000, published as 3501).

```bash
infra/observability/vm/install.sh     # once
infra/observability/vm/start.sh       # after a reboot, before ~/start-backend.sh
```

`~/start-backend.sh` on the VM exports `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318`
and uses `infra/observability/traefik.yml` for the gateway. `OTEL_SDK_DISABLED=true
~/start-backend.sh` starts the backend without telemetry; `UVICORN_WORKERS=N` runs N processes
per service. Data is kept for 14-15 days in `~/observability/data`; logs of the stack itself are in
`~/observability/logs`.
