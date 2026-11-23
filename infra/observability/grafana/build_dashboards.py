"""Generate the Grafana dashboards in ./dashboards (provisioned into the "Farm Platform" folder).

    python3 infra/observability/grafana/build_dashboards.py

Edit here, not in the JSON, so the four dashboards keep one style:
  farm-overview   service health: traffic, errors, latency per service and route (home page)
  farm-capacity   load tests: k6 users and latency next to what the servers did
  farm-infra      Postgres, Redis, RabbitMQ / events, the VM, each service process
  farm-logs       logs, error logs, traces and the service map
"""

import json
from pathlib import Path

OUT = Path(__file__).parent / "dashboards"
PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
TEMPO = {"type": "tempo", "uid": "tempo"}

# Request metrics of the services, without health checks
SVC = 'service_name=~"$service"'
HTTP = f"http_server_request_duration_seconds_bucket{{{SVC}}}"
HTTP_COUNT = f"http_server_request_duration_seconds_count{{{SVC}}}"


class Board:
    def __init__(self, uid: str, title: str, description: str, tags: list[str]):
        self.uid, self.title, self.description, self.tags = uid, title, description, tags
        self.panels: list[dict] = []
        self.y = 0
        self.x = 0
        self.row_h = 0
        self.vars: list[dict] = []
        self.next_id = 1

    # layout: panels flow left to right on a 24-wide grid
    def _place(self, w: int, h: int) -> dict:
        if self.x + w > 24:
            self.y += self.row_h
            self.x, self.row_h = 0, 0
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.row_h = max(self.row_h, h)
        return pos

    def row(self, title: str, collapsed: bool = False) -> None:
        if self.x:
            self.y += self.row_h
            self.x, self.row_h = 0, 0
        self.panels.append(
            {
                "type": "row",
                "id": self._id(),
                "title": title,
                "collapsed": collapsed,
                "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1},
                "panels": [],
            }
        )
        self.y += 1

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def add(self, panel: dict, w: int, h: int) -> None:
        panel["id"] = self._id()
        panel["gridPos"] = self._place(w, h)
        self.panels.append(panel)

    def var_query(self, name: str, label: str, query: str, multi: bool = True) -> None:
        self.vars.append(
            {
                "name": name,
                "label": label,
                "type": "query",
                "datasource": PROM,
                "query": {"query": query, "refId": "v"},
                "definition": query,
                "refresh": 2,
                "includeAll": True,
                "multi": multi,
                "allValue": ".*",
                "current": {"text": "All", "value": "$__all"},
                "sort": 1,
            }
        )

    def var_custom(self, name: str, label: str, values: list[str], default: str) -> None:
        self.vars.append(
            {
                "name": name,
                "label": label,
                "type": "custom",
                "query": ",".join(values),
                "current": {"text": default, "value": default},
                "options": [{"text": v, "value": v, "selected": v == default} for v in values],
            }
        )

    def json(self, time_from: str = "now-1h", refresh: str = "10s", links: bool = True) -> dict:
        return {
            "uid": self.uid,
            "title": self.title,
            "description": self.description,
            "tags": ["farm", *self.tags],
            "timezone": "browser",
            "editable": True,
            "graphTooltip": 1,  # shared crosshair
            "schemaVersion": 39,
            "version": 1,
            "refresh": refresh,
            "time": {"from": time_from, "to": "now"},
            "templating": {"list": self.vars},
            "annotations": {"list": []},
            "links": [
                {
                    "type": "dashboards",
                    "tags": ["farm"],
                    "asDropdown": False,
                    "title": "Farm dashboards",
                    "includeVars": False,
                    "keepTime": True,
                }
            ]
            if links
            else [],
            "panels": self.panels,
        }


def target(expr: str, legend: str = "", ds: dict = PROM, ref: str = "A", **extra) -> dict:
    t = {"datasource": ds, "expr": expr, "legendFormat": legend or "__auto", "refId": ref}
    t.update(extra)
    return t


def timeseries(
    title: str,
    targets: list[dict],
    unit: str = "short",
    description: str = "",
    stack: bool = False,
    min0: bool = True,
    overrides: list | None = None,
    thresholds: list | None = None,
    fill: int = 12,
    max_: float | None = None,
) -> dict:
    defaults: dict = {
        "unit": unit,
        "custom": {
            "drawStyle": "line",
            "lineWidth": 2,
            "fillOpacity": fill,
            "gradientMode": "opacity",
            "showPoints": "never",
            "spanNulls": True,
            "stacking": {"mode": "normal" if stack else "none", "group": "A"},
            "axisSoftMin": 0 if min0 else None,
        },
        "color": {"mode": "palette-classic"},
    }
    if max_ is not None:
        defaults["max"] = max_
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
        defaults["custom"]["thresholdsStyle"] = {"mode": "dashed"}
    return {
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": targets[0]["datasource"],
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {
            "legend": {"displayMode": "table", "placement": "bottom", "calcs": ["mean", "max"]},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def stat(
    title: str,
    expr: str,
    unit: str = "short",
    steps: list | None = None,
    description: str = "",
    decimals: int | None = None,
    spark: bool = True,
    ds: dict = PROM,
) -> dict:
    defaults: dict = {
        "unit": unit,
        "color": {"mode": "thresholds"},
        "thresholds": {
            "mode": "absolute",
            "steps": steps or [{"color": "text", "value": None}],
        },
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": ds,
        "targets": [target(expr, ds=ds, instant=False)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "graphMode": "area" if spark else "none",
            "colorMode": "value",
            "textMode": "value",
            "justifyMode": "center",
        },
    }


def table(title: str, targets: list[dict], description: str = "", overrides=None) -> dict:
    return {
        "type": "table",
        "title": title,
        "description": description,
        "datasource": PROM,
        "targets": [dict(t, instant=True, format="table", range=False) for t in targets],
        "transformations": [
            {"id": "merge", "options": {}},
            {"id": "organize", "options": {"excludeByName": {"Time": True}}},
        ],
        "fieldConfig": {"defaults": {"custom": {"align": "auto"}}, "overrides": overrides or []},
        "options": {"showHeader": True, "cellHeight": "sm"},
    }


def text(title: str, content: str) -> dict:
    return {"type": "text", "title": title, "options": {"mode": "markdown", "content": content}}


GREEN_AMBER_RED = lambda a, r: [  # noqa: E731
    {"color": "green", "value": None},
    {"color": "#EAB839", "value": a},
    {"color": "red", "value": r},
]


def by_unit(field: str, unit: str) -> dict:
    return {
        "matcher": {"id": "byName", "options": field},
        "properties": [{"id": "unit", "value": unit}],
    }


def right_axis(regex: str, unit: str) -> dict:
    return {
        "matcher": {"id": "byRegexp", "options": regex},
        "properties": [
            {"id": "custom.axisPlacement", "value": "right"},
            {"id": "unit", "value": unit},
            {"id": "custom.fillOpacity", "value": 0},
            {"id": "custom.lineStyle", "value": {"dash": [6, 4], "fill": "dash"}},
        ],
    }


def q(p: float, by: str, sel: str = HTTP, window: str = "$__rate_interval") -> str:
    return f"histogram_quantile({p}, sum by (le{', ' + by if by else ''}) (rate({sel}[{window}])))"


# --------------------------------------------------------------------------------------------
def overview() -> dict:
    b = Board(
        "farm-overview",
        "Farm · Service health",
        "Traffic, errors and latency (RED) for every Farm service. Start here.",
        ["overview"],
    )
    b.var_query("service", "Service", "label_values(http_server_request_duration_seconds_count, service_name)")
    b.var_query(
        "route",
        "Route",
        'label_values(http_server_request_duration_seconds_count{service_name=~"$service"}, http_route)',
    )
    sel = f'{SVC}, http_route=~"$route"'
    bucket = f"http_server_request_duration_seconds_bucket{{{sel}}}"
    count = f"http_server_request_duration_seconds_count{{{sel}}}"
    err = f'{{{sel}, http_response_status_code=~"5.."}}'

    b.row("At a glance")
    b.add(stat("Requests / s", f"sum(rate({count}[$__rate_interval]))", "reqps", decimals=1), 4, 4)
    b.add(
        stat(
            "Server errors (5xx)",
            f"(sum(rate(http_server_request_duration_seconds_count{err}[$__rate_interval])) or vector(0))"
            f" / clamp_min(sum(rate({count}[$__rate_interval])), 1e-9)",
            "percentunit",
            GREEN_AMBER_RED(0.001, 0.01),
            "Share of requests answered with a 5xx. Goal: under 1%.",
            decimals=2,
        ),
        4,
        4,
    )
    b.add(
        stat(
            "Client errors (4xx)",
            f'(sum(rate(http_server_request_duration_seconds_count{{{sel}, http_response_status_code=~"4.."}}[$__rate_interval])) or vector(0))'
            f" / clamp_min(sum(rate({count}[$__rate_interval])), 1e-9)",
            "percentunit",
            GREEN_AMBER_RED(0.05, 0.2),
            "Validation errors, 401/403/404/409 and rate limiting (429).",
            decimals=1,
        ),
        4,
        4,
    )
    b.add(
        stat("p50 latency", q(0.5, "", bucket), "s", GREEN_AMBER_RED(0.1, 0.3), decimals=3), 4, 4
    )
    b.add(
        stat(
            "p95 latency",
            q(0.95, "", bucket),
            "s",
            GREEN_AMBER_RED(0.3, 0.8),
            "95% of requests finish faster than this. Goal: under 500 ms.",
            decimals=3,
        ),
        4,
        4,
    )
    b.add(
        stat("p99 latency", q(0.99, "", bucket), "s", GREEN_AMBER_RED(0.5, 1.5), decimals=3), 4, 4
    )

    b.row("Traffic")
    b.add(
        timeseries(
            "Requests / s by service",
            [target(f"sum by (service_name) (rate({count}[$__rate_interval]))", "{{service_name}}")],
            "reqps",
            stack=True,
        ),
        12,
        8,
    )
    b.add(
        timeseries(
            "Responses by status code",
            [
                target(
                    f"sum by (http_response_status_code) (rate({count}[$__rate_interval]))",
                    "{{http_response_status_code}}",
                )
            ],
            "reqps",
            stack=True,
            overrides=[
                {
                    "matcher": {"id": "byRegexp", "options": "5.."},
                    "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}],
                },
                {
                    "matcher": {"id": "byName", "options": "429"},
                    "properties": [
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}}
                    ],
                },
            ],
        ),
        12,
        8,
    )

    b.row("Latency")
    b.add(
        timeseries(
            "p95 latency by service",
            [target(q(0.95, "service_name", bucket), "{{service_name}}")],
            "s",
            thresholds=[{"color": "transparent", "value": None}, {"color": "red", "value": 0.5}],
            description="Dashed red line: the 500 ms goal.",
            fill=0,
        ),
        12,
        8,
    )
    b.add(
        timeseries(
            "Latency percentiles (all selected)",
            [
                target(q(0.5, "", bucket), "p50", ref="A"),
                target(q(0.9, "", bucket), "p90", ref="B"),
                target(q(0.95, "", bucket), "p95", ref="C"),
                target(q(0.99, "", bucket), "p99", ref="D"),
            ],
            "s",
            fill=0,
        ),
        12,
        8,
    )
    b.add(
        {
            "type": "heatmap",
            "title": "Latency distribution",
            "description": "Where requests fall in time. A band moving up = everything slowing down.",
            "datasource": PROM,
            "targets": [
                target(
                    f"sum by (le) (increase({bucket}[$__rate_interval]))",
                    "{{le}}",
                    format="heatmap",
                )
            ],
            "options": {
                "calculate": False,
                "yAxis": {"unit": "s"},
                "color": {"scheme": "Oranges", "mode": "scheme", "steps": 64},
                "cellGap": 1,
                "tooltip": {"mode": "single", "yHistogram": True},
                "exemplars": {"color": "rgba(255,0,255,0.7)"},
            },
            "fieldConfig": {"defaults": {}, "overrides": []},
        },
        12,
        8,
    )
    b.add(
        timeseries(
            "Error rate by service (5xx)",
            [
                target(
                    f"sum by (service_name) (rate(http_server_request_duration_seconds_count{err}[$__rate_interval]))"
                    f" / sum by (service_name) (rate({count}[$__rate_interval]))",
                    "{{service_name}}",
                )
            ],
            "percentunit",
            fill=0,
        ),
        12,
        8,
    )

    b.row("Routes")
    rate_by_route = (
        f"sum by (service_name, http_request_method, http_route) (rate({count}[$__range]))"
    )
    b.add(
        table(
            "Routes: traffic, latency and errors (selected time range)",
            [
                target(rate_by_route, ref="A"),
                target(
                    q(0.95, "service_name, http_request_method, http_route", bucket, "$__range"),
                    ref="B",
                ),
                target(
                    q(0.99, "service_name, http_request_method, http_route", bucket, "$__range"),
                    ref="C",
                ),
                target(
                    "sum by (service_name, http_request_method, http_route) "
                    f"(rate(http_server_request_duration_seconds_count{err}[$__range]))"
                    f" / {rate_by_route}",
                    ref="D",
                ),
            ],
            "Sorted by p95. Click a column header to sort differently.",
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "Value #A"},
                    "properties": [
                        {"id": "displayName", "value": "req/s"},
                        {"id": "unit", "value": "reqps"},
                        {"id": "decimals", "value": 2},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "Value #B"},
                    "properties": [
                        {"id": "displayName", "value": "p95"},
                        {"id": "unit", "value": "s"},
                        {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                        {
                            "id": "thresholds",
                            "value": {"mode": "absolute", "steps": GREEN_AMBER_RED(0.3, 0.8)},
                        },
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "Value #C"},
                    "properties": [
                        {"id": "displayName", "value": "p99"},
                        {"id": "unit", "value": "s"},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "Value #D"},
                    "properties": [
                        {"id": "displayName", "value": "5xx"},
                        {"id": "unit", "value": "percentunit"},
                        {"id": "noValue", "value": "0"},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "service_name"},
                    "properties": [{"id": "displayName", "value": "service"}],
                },
                {
                    "matcher": {"id": "byName", "options": "http_request_method"},
                    "properties": [{"id": "displayName", "value": "method"}],
                },
                {
                    "matcher": {"id": "byName", "options": "http_route"},
                    "properties": [{"id": "displayName", "value": "route"}],
                },
            ],
        )
        | {"options": {"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "p95", "desc": True}]}},
        24,
        12,
    )

    b.row("Gateway (Traefik)")
    b.add(
        timeseries(
            "Gateway requests / s by code",
            [
                target(
                    'sum by (code) (rate(traefik_entrypoint_requests_total{entrypoint="web"}[$__rate_interval]))',
                    "{{code}}",
                )
            ],
            "reqps",
            stack=True,
            description="Everything that reaches the gateway, including 429 from rate limiting.",
        ),
        12,
        7,
    )
    b.add(
        timeseries(
            "Rate-limited requests (429) by router",
            [
                target(
                    'sum by (router) (rate(traefik_router_requests_total{code="429"}[$__rate_interval]))',
                    "{{router}}",
                )
            ],
            "reqps",
            description="Requests turned away by the gateway's per-IP rate limits.",
        ),
        12,
        7,
    )
    return b.json()


# --------------------------------------------------------------------------------------------
def capacity() -> dict:
    b = Board(
        "farm-capacity",
        "Farm · Load test & capacity",
        "k6 load tests next to what the servers did: how many users the platform supports.",
        ["load-test"],
    )
    b.var_query("testid", "Test run", "label_values(k6_vus, testid)", multi=False)
    k6 = 'testid=~"$testid"'
    b.add(
        text(
            "How to read this",
            "**Users** (virtual users, k6) ramp up; watch **throughput** stop growing while "
            "**latency** climbs: that knee is the capacity. Goals: p95 under 500 ms, under 1% "
            "failed requests. The lower rows show which resource ran out first (CPU, database "
            "connections, a single service).",
        ),
        24,
        3,
    )

    b.row("Load (k6, client side)")
    b.add(stat("Virtual users", f"sum(k6_vus{{{k6}}})", spark=True), 4, 4)
    b.add(
        stat("Requests / s (k6)", f"sum(rate(k6_http_reqs_total{{{k6}}}[$__rate_interval]))", "reqps", decimals=0),
        4,
        4,
    )
    b.add(
        stat(
            "p95 (k6)",
            f"max(k6_http_req_duration_p95{{{k6}}}) / 1000",
            "s",
            GREEN_AMBER_RED(0.3, 0.5),
            decimals=3,
        ),
        4,
        4,
    )
    b.add(
        stat(
            "Failed requests",
            f"avg(k6_http_req_failed_rate{{{k6}}})",
            "percentunit",
            GREEN_AMBER_RED(0.005, 0.01),
            decimals=2,
        ),
        4,
        4,
    )
    b.add(
        stat(
            "Checks passing",
            f"avg(k6_checks_rate{{{k6}}})",
            "percentunit",
            [{"color": "red", "value": None}, {"color": "#EAB839", "value": 0.95}, {"color": "green", "value": 0.99}],
            decimals=2,
        ),
        4,
        4,
    )
    b.add(
        stat("Journeys done", f"sum(k6_iterations_total{{{k6}}})", decimals=0),
        4,
        4,
    )
    b.add(
        timeseries(
            "Users vs throughput",
            [
                target(f"sum(rate(k6_http_reqs_total{{{k6}}}[$__rate_interval]))", "requests/s", ref="A"),
                target(f"sum(k6_vus{{{k6}}})", "virtual users", ref="B"),
            ],
            "reqps",
            overrides=[right_axis("virtual users", "short")],
            description="Throughput should follow users. When it flattens while users keep rising, the system is saturated.",
        ),
        12,
        9,
    )
    b.add(
        timeseries(
            "Users vs latency (k6)",
            [
                target(f"max(k6_http_req_duration_p95{{{k6}}}) / 1000", "p95", ref="A"),
                target(f"max(k6_http_req_duration_p99{{{k6}}}) / 1000", "p99", ref="B"),
                target(f"max(k6_http_req_duration_p50{{{k6}}}) / 1000", "p50", ref="C"),
                target(f"sum(k6_vus{{{k6}}})", "virtual users", ref="D"),
            ],
            "s",
            overrides=[right_axis("virtual users", "short")],
            thresholds=[{"color": "transparent", "value": None}, {"color": "red", "value": 0.5}],
            fill=0,
        ),
        12,
        9,
    )
    b.add(
        timeseries(
            "p95 by request (k6)",
            [target(f"max by (name) (k6_http_req_duration_p95{{{k6}}}) / 1000", "{{name}}")],
            "s",
            fill=0,
            description="Grouped by the request name the test gives each call.",
        ),
        12,
        9,
    )
    b.add(
        timeseries(
            "Failed requests by status (k6)",
            [
                target(
                    f'sum by (status) (rate(k6_http_reqs_total{{{k6}, expected_response="false"}}[$__rate_interval]))',
                    "{{status}}",
                )
            ],
            "reqps",
            stack=True,
            description="Status 0 means the request never got an answer (timeout or connection refused).",
        ),
        12,
        9,
    )

    b.row("Servers (what the platform did)")
    b.add(
        timeseries(
            "Server throughput by service",
            [
                target(
                    "sum by (service_name) (rate(http_server_request_duration_seconds_count[$__rate_interval]))",
                    "{{service_name}}",
                )
            ],
            "reqps",
            stack=True,
        ),
        12,
        8,
    )
    b.add(
        timeseries(
            "Server p95 by service",
            [
                target(
                    q(0.95, "service_name", "http_server_request_duration_seconds_bucket"),
                    "{{service_name}}",
                )
            ],
            "s",
            fill=0,
            thresholds=[{"color": "transparent", "value": None}, {"color": "red", "value": 0.5}],
        ),
        12,
        8,
    )
    b.add(
        timeseries(
            "VM CPU used (of the container's limit)",
            [
                target(
                    "rate(vm_cgroup_cpu_usage_seconds_total[$__rate_interval]) / vm_cgroup_cpu_limit_cores",
                    "CPU used",
                    ref="A",
                ),
                target(
                    "rate(vm_cgroup_cpu_throttled_seconds_total[$__rate_interval]) / vm_cgroup_cpu_limit_cores",
                    "throttled",
                    ref="B",
                ),
            ],
            "percentunit",
            description="The VM is a container limited to vm_cgroup_cpu_limit_cores CPUs. 'throttled' means work had to wait for CPU.",
            max_=1,
            thresholds=[{"color": "transparent", "value": None}, {"color": "red", "value": 0.85}],
        ),
        8,
        8,
    )
    b.add(
        timeseries(
            "CPU by process (cores)",
            [
                target(
                    "sum by (service_name) (rate(process_cpu_time_seconds_total[$__rate_interval]))",
                    "{{service_name}}",
                ),
            ],
            "short",
            stack=True,
            description="One Python service process can use at most 1 core. A service flat at 1.0 is saturated.",
        ),
        8,
        8,
    )
    b.add(
        timeseries(
            "Database connections",
            [
                target("sum by (datname) (pg_stat_activity_count)", "{{datname}}", ref="A"),
                target("max(pg_settings_max_connections)", "max_connections", ref="B"),
            ],
            "short",
            stack=False,
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "max_connections"},
                    "properties": [
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}},
                        {"id": "custom.fillOpacity", "value": 0},
                        {"id": "custom.lineStyle", "value": {"dash": [6, 4], "fill": "dash"}},
                    ],
                }
            ],
        ),
        8,
        8,
    )

    b.row("Business throughput")
    biz = [
        ("Orders placed / min", 'service_name="order", http_route="/orders", http_request_method="POST", http_response_status_code=~"2.."'),
        ("Logins / min", 'service_name="identity", http_route="/login", http_response_status_code=~"2.."'),
        ("Sign-ups / min", 'service_name="identity", http_route="/register", http_response_status_code=~"2.."'),
        ("Produce searches / min", 'service_name="catalog", http_route="/listings/search"'),
        ("Land searches / min", 'service_name="land", http_route="/plots/search"'),
        ("Listings created / min", 'service_name="catalog", http_route="/listings", http_request_method="POST", http_response_status_code=~"2.."'),
    ]
    for title, sel in biz:
        b.add(
            stat(title, f"sum(rate(http_server_request_duration_seconds_count{{{sel}}}[$__rate_interval])) * 60", decimals=0),
            4,
            4,
        )
    return b.json(time_from="now-30m", refresh="5s")


# --------------------------------------------------------------------------------------------
def infra() -> dict:
    b = Board(
        "farm-infra",
        "Farm · Infrastructure",
        "Database, cache, message broker, background jobs and the machine.",
        ["infra"],
    )
    b.row("Machine (VM container)")
    b.add(
        stat(
            "CPU used",
            "rate(vm_cgroup_cpu_usage_seconds_total[$__rate_interval]) / vm_cgroup_cpu_limit_cores",
            "percentunit",
            GREEN_AMBER_RED(0.6, 0.85),
            decimals=0,
        ),
        4,
        4,
    )
    b.add(stat("CPU limit", "vm_cgroup_cpu_limit_cores", "short", spark=False, description="Cores this container may use."), 4, 4)
    b.add(
        stat(
            "Memory used",
            "vm_cgroup_memory_bytes / vm_cgroup_memory_limit_bytes",
            "percentunit",
            GREEN_AMBER_RED(0.7, 0.9),
            "Includes page cache, which the kernel frees when needed.",
            decimals=0,
        ),
        4,
        4,
    )
    b.add(stat("Memory limit", "vm_cgroup_memory_limit_bytes", "bytes", spark=False), 4, 4)
    b.add(
        stat(
            "Disk free (home)",
            'node_filesystem_avail_bytes{mountpoint=~"/home.*|/"} / node_filesystem_size_bytes{mountpoint=~"/home.*|/"}',
            "percentunit",
            [{"color": "red", "value": None}, {"color": "#EAB839", "value": 0.1}, {"color": "green", "value": 0.2}],
            decimals=0,
            spark=False,
        ),
        4,
        4,
    )
    b.add(
        stat("Postgres up", "pg_up", spark=False, steps=[{"color": "red", "value": None}, {"color": "green", "value": 1}]),
        4,
        4,
    )
    b.add(
        timeseries(
            "CPU (cores)",
            [
                target("rate(vm_cgroup_cpu_usage_seconds_total[$__rate_interval])", "used", ref="A"),
                target("vm_cgroup_cpu_limit_cores", "limit", ref="B"),
                target("rate(vm_cgroup_cpu_throttled_seconds_total[$__rate_interval])", "throttled", ref="C"),
            ],
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "limit"},
                    "properties": [
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}},
                        {"id": "custom.fillOpacity", "value": 0},
                    ],
                }
            ],
        ),
        12,
        8,
    )
    b.add(
        timeseries(
            "Memory",
            [
                target("vm_cgroup_memory_bytes", "used (incl. cache)", ref="A"),
                target("vm_cgroup_memory_anon_bytes", "used by programs", ref="B"),
                target("vm_cgroup_memory_limit_bytes", "limit", ref="C"),
            ],
            "bytes",
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "limit"},
                    "properties": [
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}},
                        {"id": "custom.fillOpacity", "value": 0},
                    ],
                }
            ],
        ),
        12,
        8,
    )

    b.row("Service processes")
    b.add(
        timeseries(
            "CPU by service (cores)",
            [target("sum by (service_name) (rate(process_cpu_time_seconds_total[$__rate_interval]))", "{{service_name}}")],
            stack=True,
        ),
        8,
        8,
    )
    b.add(
        timeseries(
            "Memory by service (RSS)",
            [target("sum by (service_name) (process_memory_usage_bytes)", "{{service_name}}")],
            "bytes",
            stack=True,
        ),
        8,
        8,
    )
    b.add(
        timeseries(
            "Requests in flight",
            [target("sum by (service_name) (http_server_active_requests)", "{{service_name}}")],
            description="Requests being handled right now. Growing steadily = the service cannot keep up.",
        ),
        8,
        8,
    )

    b.row("PostgreSQL")
    b.add(
        timeseries(
            "Connections by state",
            [
                target('sum by (state) (pg_stat_activity_count{datname="farm"})', "{{state}}", ref="A"),
                target("max(pg_settings_max_connections)", "max_connections", ref="B"),
            ],
            stack=False,
        ),
        8,
        8,
    )
    b.add(
        timeseries(
            "Transactions / s",
            [
                target('sum(rate(pg_stat_database_xact_commit{datname="farm"}[$__rate_interval]))', "commits", ref="A"),
                target('sum(rate(pg_stat_database_xact_rollback{datname="farm"}[$__rate_interval]))', "rollbacks", ref="B"),
            ],
            "ops",
        ),
        8,
        8,
    )
    b.add(
        timeseries(
            "Cache hit ratio",
            [
                target(
                    'sum(rate(pg_stat_database_blks_hit{datname="farm"}[$__rate_interval])) / '
                    '(sum(rate(pg_stat_database_blks_hit{datname="farm"}[$__rate_interval])) + sum(rate(pg_stat_database_blks_read{datname="farm"}[$__rate_interval])))',
                    "hit ratio",
                )
            ],
            "percentunit",
            min0=False,
        ),
        8,
        8,
    )
    b.add(
        timeseries(
            "Rows / s",
            [
                target('sum(rate(pg_stat_database_tup_fetched{datname="farm"}[$__rate_interval]))', "fetched", ref="A"),
                target('sum(rate(pg_stat_database_tup_inserted{datname="farm"}[$__rate_interval]))', "inserted", ref="B"),
                target('sum(rate(pg_stat_database_tup_updated{datname="farm"}[$__rate_interval]))', "updated", ref="C"),
                target('sum(rate(pg_stat_database_tup_deleted{datname="farm"}[$__rate_interval]))', "deleted", ref="D"),
            ],
            "rowsps",
        ),
        8,
        8,
    )
    b.add(
        timeseries(
            "Locks and deadlocks",
            [
                target('sum by (mode) (pg_locks_count{datname="farm"})', "{{mode}}", ref="A"),
                target('sum(increase(pg_stat_database_deadlocks{datname="farm"}[$__rate_interval]))', "deadlocks", ref="B"),
            ],
        ),
        8,
        8,
    )
    b.add(
        timeseries(
            "Longest open transaction",
            [target('max(pg_stat_activity_max_tx_duration{datname="farm"})', "longest")],
            "s",
        ),
        8,
        8,
    )

    b.row("Events (RabbitMQ) and background jobs")
    b.add(
        timeseries(
            "Events published / s by type",
            [target("sum by (event_type) (rate(farm_events_published_total[$__rate_interval]))", "{{event_type}}")],
            "ops",
            stack=True,
        ),
        8,
        8,
    )
    b.add(
        timeseries(
            "Events handled / s by outcome",
            [target("sum by (outcome) (rate(farm_events_consumed_total[$__rate_interval]))", "{{outcome}}")],
            "ops",
            stack=True,
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "dead_letter"},
                    "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}],
                },
                {
                    "matcher": {"id": "byName", "options": "retry"},
                    "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}}],
                },
            ],
        ),
        8,
        8,
    )
    b.add(
        timeseries(
            "Event handler p95 by queue",
            [target(q(0.95, "queue", "farm_events_handler_duration_seconds_bucket"), "{{queue}}")],
            "s",
            fill=0,
        ),
        8,
        8,
    )
    b.add(
        timeseries(
            "Messages waiting by queue",
            [
                target(
                    'sum by (queue) (rabbitmq_queue_messages_ready{queue!~".*retry.*"}) > 0',
                    "{{queue}}",
                )
            ],
            description="A backlog that keeps growing means a consumer cannot keep up.",
            stack=True,
        ),
        8,
        8,
    )
    b.add(
        timeseries(
            "Dead-letter queues",
            [target('sum by (queue) (rabbitmq_queue_messages{queue=~".*dlq"})', "{{queue}}")],
            description="Events that failed every retry. Should stay at 0; see docs/events-dlq.md.",
        ),
        8,
        8,
    )
    b.add(
        timeseries(
            "Background jobs p95 (pooling, media)",
            [target(q(0.95, "job", "farm_jobs_duration_seconds_bucket"), "{{job}}")],
            "s",
            fill=0,
        ),
        8,
        8,
    )

    b.row("Redis")
    b.add(
        timeseries("Commands / s", [target("sum(rate(redis_commands_processed_total[$__rate_interval]))", "commands")], "ops"),
        8,
        7,
    )
    b.add(timeseries("Memory", [target("redis_memory_used_bytes", "used")], "bytes"), 8, 7)
    b.add(
        timeseries(
            "Clients and keys",
            [
                target("redis_connected_clients", "clients", ref="A"),
                target("sum(redis_db_keys)", "keys", ref="B"),
            ],
        ),
        8,
        7,
    )

    b.row("Telemetry pipeline", collapsed=False)
    b.add(
        timeseries(
            "Collector throughput",
            [
                target("sum(rate(otelcol_receiver_accepted_spans[$__rate_interval]))", "spans in", ref="A"),
                target("sum(rate(otelcol_receiver_accepted_log_records[$__rate_interval]))", "log lines in", ref="B"),
                target("sum(rate(otelcol_receiver_accepted_metric_points[$__rate_interval]))", "metric points in", ref="C"),
                target("sum(rate(otelcol_receiver_failed_spans[$__rate_interval]))", "spans refused", ref="D"),
            ],
            "ops",
        ),
        24,
        7,
    )
    return b.json()


# --------------------------------------------------------------------------------------------
def logs_traces() -> dict:
    b = Board(
        "farm-logs",
        "Farm · Logs & traces",
        "Search logs and traces. Click a trace id in a log line to open the trace, and a span to see its logs.",
        ["logs", "traces"],
    )
    b.var_query("service", "Service", "label_values(http_server_request_duration_seconds_count, service_name)")
    b.vars.append(
        {
            "name": "search",
            "label": "Log text contains",
            "type": "textbox",
            "query": "",
            "current": {"text": "", "value": ""},
        }
    )
    b.var_custom("level", "Level", ["INFO|WARNING|ERROR|CRITICAL", "WARNING|ERROR|CRITICAL", "ERROR|CRITICAL"], "INFO|WARNING|ERROR|CRITICAL")
    lsel = '{service_name=~"$service"}'
    b.row("Logs")
    b.add(
        timeseries(
            "Log lines by level",
            [
                target(
                    f'sum by (detected_level) (count_over_time({lsel} [$__auto]))',
                    "{{detected_level}}",
                    ds=LOKI,
                    queryType="range",
                )
            ],
            stack=True,
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "error"},
                    "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}],
                },
                {
                    "matcher": {"id": "byName", "options": "warn"},
                    "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}}],
                },
            ],
        )
        | {"datasource": LOKI},
        12,
        7,
    )
    b.add(
        timeseries(
            "Errors by service",
            [
                target(
                    f'sum by (service_name) (count_over_time({{service_name=~"$service", severity_text=~"ERROR|CRITICAL"}} [$__auto]))',
                    "{{service_name}}",
                    ds=LOKI,
                    queryType="range",
                )
            ],
            stack=True,
        )
        | {"datasource": LOKI},
        12,
        7,
    )
    b.add(
        {
            "type": "logs",
            "title": "Logs",
            "description": "Newest first. Expand a line and use 'Open trace' to see the whole request.",
            "datasource": LOKI,
            "targets": [
                {
                    "datasource": LOKI,
                    "refId": "A",
                    "expr": f'{lsel} | severity_text=~"$level" |= "$search"',
                    "queryType": "range",
                }
            ],
            "options": {
                "showTime": True,
                "wrapLogMessage": True,
                "sortOrder": "Descending",
                "enableLogDetails": True,
                "prettifyLogMessage": False,
                "dedupStrategy": "none",
            },
        },
        24,
        14,
    )

    b.row("Traces")
    b.add(
        {
            "type": "nodeGraph",
            "title": "Service map (from traces)",
            "description": "Who calls whom, with request rate and errors, built by Tempo from the spans.",
            "datasource": TEMPO,
            "targets": [{"datasource": TEMPO, "refId": "A", "queryType": "serviceMap"}],
        },
        12,
        12,
    )
    b.add(
        {
            "type": "table",
            "title": "Slowest recent requests (over 500 ms)",
            "description": "Click a trace id to open it.",
            "datasource": TEMPO,
            "targets": [
                {
                    "datasource": TEMPO,
                    "refId": "A",
                    "queryType": "traceql",
                    "query": '{ span.http.route != nil && duration > 500ms }',
                    "limit": 20,
                    "tableType": "traces",
                }
            ],
        },
        12,
        12,
    )
    b.add(
        {
            "type": "table",
            "title": "Failed requests and events (errors)",
            "datasource": TEMPO,
            "targets": [
                {
                    "datasource": TEMPO,
                    "refId": "A",
                    "queryType": "traceql",
                    "query": "{ status = error }",
                    "limit": 20,
                    "tableType": "traces",
                }
            ],
        },
        12,
        10,
    )
    b.add(
        {
            "type": "table",
            "title": "Event flows (requests that published events)",
            "datasource": TEMPO,
            "targets": [
                {
                    "datasource": TEMPO,
                    "refId": "A",
                    "queryType": "traceql",
                    "query": "{ kind = producer } >> { kind = consumer } | count() > 0",
                    "limit": 20,
                    "tableType": "traces",
                }
            ],
        },
        12,
        10,
    )
    return b.json(refresh="30s")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, build in {
        "farm-overview": overview,
        "farm-capacity": capacity,
        "farm-infra": infra,
        "farm-logs": logs_traces,
    }.items():
        (OUT / f"{name}.json").write_text(json.dumps(build(), indent=2) + "\n")
        print("wrote", OUT / f"{name}.json")


if __name__ == "__main__":
    main()
