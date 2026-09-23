"""Synthetic ECS log generator for the shopdemo observability demo.

Simulates request flows across four services (checkout, payment, auth,
inventory). All logs of one request share a trace.id. Optionally injects
one of three incidents. Logs are backfilled over a past time window.

Examples:
    python log_generator/generator.py --scenario none --output stdout
    python log_generator/generator.py --scenario db_timeout --output file --file logs.ndjson
    python log_generator/generator.py --scenario memory_leak --output es
"""

import argparse
import json
import os
import random
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

DATA_STREAM = "logs-shopdemo-default"
SCENARIOS = ["none", "db_timeout", "auth_cert_expired", "memory_leak"]

# Normal behaviour per service.
# "error" tuple: (error.type, message template, HTTP status code)
SERVICES = {
    "checkout": {
        "version": "2.1.0",
        "path": "/api/checkout/order",
        "info": "Order created (order_id={id})",
        "warn": "Cart validation slow ({ms}ms)",
        "error": ("InvalidCartError", "Item no longer available (sku={id})", 409),
    },
    "payment": {
        "version": "1.4.2",
        "path": "/api/payment/charge",
        "info": "Payment authorized (payment_id={id})",
        "warn": "Payment provider response slow ({ms}ms)",
        "error": ("CardDeclinedError", "Card declined by issuer (payment_id={id})", 402),
    },
    "auth": {
        "version": "3.0.1",
        "path": "/api/auth/validate",
        "info": "Token validated (session_id={id})",
        "warn": "Token validation slow ({ms}ms)",
        "error": ("InvalidTokenError", "Token rejected: session expired (session_id={id})", 401),
    },
    "inventory": {
        "version": "1.8.0",
        "path": "/api/inventory/reserve",
        "info": "Stock reserved (sku={id})",
        "warn": "Stock lookup slow ({ms}ms, sku={id})",
        "error": ("StockNotFoundError", "Unknown sku (sku={id})", 404),
    },
}

# Which services an entry service calls before doing its own work
CALL_CHAINS = {
    "checkout": ["auth", "inventory", "payment"],
    "inventory": ["auth"],
    "payment": ["auth"],
}
ENTRY_WEIGHTS = {"checkout": 60, "inventory": 20, "payment": 20}

USER_EMAILS = [
    "anna.mueller@example.de",
    "jan.schmidt@example.de",
    "lena.fischer@example.de",
    "tobias.weber@example.de",
    "sara.hoffmann@example.de",
    "david.koch@example.de",
]

# Incident parameters
CERT_CN = "auth.shopdemo.internal"
DB_POOL_SIZE = 50
MEMORY_LIMIT_MB = 2048
MEMORY_BASELINE_MB = 550
LEAK_GROWTH_MINUTES = 20   # from leak start (or restart) to OOM crash
RESTART_MINUTES = 2        # inventory is down this long after a crash


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def to_ecs_time(dt: datetime) -> str:
    """Format a datetime as ECS timestamp (UTC, milliseconds), e.g. 2026-09-23T14:02:11.482Z"""
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_context() -> dict:
    """Request context shared by all logs of one request flow."""
    return {
        "trace_id": f"{random.getrandbits(128):032x}",  # 32 hex chars, W3C trace id format
        "email": random.choice(USER_EMAILS),
        "ip": f"192.168.{random.randint(1, 254)}.{random.randint(1, 254)}",
    }


def base_log(service: str, ts: datetime, ctx: dict, level: str, message: str,
             duration_ms: int, status: int = 200) -> dict:
    cfg = SERVICES[service]
    return {
        "@timestamp": to_ecs_time(ts),
        "log": {"level": level},
        "service": {"name": service, "version": cfg["version"]},
        "host": {"name": f"{service}-{random.randint(1, 2):02d}"},
        "trace": {"id": ctx["trace_id"]},
        "event": {"duration": duration_ms * 1_000_000},  # ECS: nanoseconds
        "http": {
            "request": {"method": "POST"},
            "response": {"status_code": status},
        },
        "url": {"path": cfg["path"]},
        "user": {"email": ctx["email"]},
        "source": {"ip": ctx["ip"]},
        "message": message,
    }


def add_error(log: dict, error_type: str) -> dict:
    log["error"] = {"type": error_type, "message": log["message"]}
    return log


def duration_ms_of(log: dict) -> int:
    return log["event"]["duration"] // 1_000_000


def is_failure(log: dict) -> bool:
    return log["log"]["level"] in ("error", "critical")


# --------------------------------------------------------------------------
# Normal (baseline) behaviour
# --------------------------------------------------------------------------

def make_normal_log(service: str, ts: datetime, ctx: dict) -> dict:
    """One log with baseline behaviour: ~95% info, ~4% warn, ~1% error."""
    cfg = SERVICES[service]
    level = random.choices(["info", "warn", "error"], weights=[95, 4, 1])[0]
    duration_ms = random.randint(800, 1500) if level == "warn" else random.randint(50, 300)
    entity_id = random.randint(10000, 99999)

    if level == "error":
        error_type, template, status = cfg["error"]
        msg = template.format(id=entity_id, ms=duration_ms)
        return add_error(base_log(service, ts, ctx, "error", msg, duration_ms, status), error_type)

    msg = cfg[level].format(id=entity_id, ms=duration_ms)
    return base_log(service, ts, ctx, level, msg, duration_ms)


# --------------------------------------------------------------------------
# Incident behaviour (overrides normal behaviour of the root-cause service)
# --------------------------------------------------------------------------

def db_timeout_logs(ts: datetime, ctx: dict, state: dict) -> list[dict] | None:
    """payment: pool pressure warnings, then DB timeouts."""
    start = state["incident_start"]
    if start - timedelta(minutes=10) <= ts < start and random.random() < 0.3:
        used = random.randint(40, DB_POOL_SIZE - 1)
        msg = f"DB connection pool usage high ({used}/{DB_POOL_SIZE})"
        return [base_log("payment", ts, ctx, "warn", msg, random.randint(800, 1500))]
    if ts >= start and random.random() < 0.85:
        msg = (f"Timeout acquiring connection from pool payment-db "
               f"(active={DB_POOL_SIZE}/{DB_POOL_SIZE}, waited 5000ms)")
        log = base_log("payment", ts, ctx, "error", msg, 5000, 500)
        return [add_error(log, "DatabaseTimeoutError")]
    return None


def auth_cert_logs(ts: datetime, ctx: dict, state: dict) -> list[dict] | None:
    """auth: rare expiry warnings, then every TLS handshake fails."""
    start = state["incident_start"]
    not_after = to_ecs_time(start)
    if ts < start and random.random() < 0.01:
        msg = f"TLS certificate expires soon (CN={CERT_CN}, notAfter={not_after})"
        return [base_log("auth", ts, ctx, "warn", msg, random.randint(50, 300))]
    if ts >= start:
        msg = f"TLS handshake failed: certificate has expired (CN={CERT_CN}, notAfter={not_after})"
        log = base_log("auth", ts, ctx, "error", msg, random.randint(20, 50), 500)
        return [add_error(log, "CertificateExpiredError")]
    return None


def inventory_memory_logs(ts: datetime, ctx: dict, state: dict) -> list[dict]:
    """inventory: memory grows until OOM crash, short downtime, restart, repeat.

    Returns [] while inventory is down (no process, no logs)."""
    start = state["incident_start"]
    leaking = state["scenario"] == "memory_leak" and ts >= start

    if not leaking:
        logs = [make_normal_log("inventory", ts, ctx)]
        used = MEMORY_BASELINE_MB + random.randint(-50, 50)
    else:
        cycle_len = LEAK_GROWTH_MINUTES + RESTART_MINUTES
        elapsed = (ts - start).total_seconds() / 60
        cycle, pos = int(elapsed // cycle_len), elapsed % cycle_len
        logs = []

        if pos >= LEAK_GROWTH_MINUTES:  # crash / down window
            if cycle in state["oom_logged"]:
                return []
            state["oom_logged"].add(cycle)
            msg = f"Process killed: out of memory (used={MEMORY_LIMIT_MB}MB, limit={MEMORY_LIMIT_MB}MB)"
            log = add_error(base_log("inventory", ts, ctx, "critical", msg,
                                     random.randint(2000, 4000), 500), "OutOfMemoryError")
            log["shopdemo"] = {"memory": {"used_mb": MEMORY_LIMIT_MB, "limit_mb": MEMORY_LIMIT_MB}}
            return [log]

        if cycle > 0 and cycle not in state["restart_logged"]:
            state["restart_logged"].add(cycle)
            logs.append(base_log("inventory", ts, ctx, "info",
                                 "Service restarted (memory reset)", random.randint(50, 100)))

        used = int(MEMORY_BASELINE_MB + (MEMORY_LIMIT_MB - MEMORY_BASELINE_MB)
                   * pos / LEAK_GROWTH_MINUTES) + random.randint(-20, 20)
        used = min(used, MEMORY_LIMIT_MB - 1)
        pct = round(used / MEMORY_LIMIT_MB * 100)

        if pct >= 85 and random.random() < 0.4:
            gc_s = round(random.uniform(1.0, 3.0), 1)
            msg = f"Long GC pause detected ({gc_s}s), response time increasing"
            logs.append(base_log("inventory", ts, ctx, "warn", msg, random.randint(1500, 4000)))
        elif pct >= 75 and random.random() < 0.3:
            msg = f"Memory usage at {pct}% ({used}MB/{MEMORY_LIMIT_MB}MB)"
            logs.append(base_log("inventory", ts, ctx, "warn", msg, random.randint(800, 1500)))
        else:
            logs.append(make_normal_log("inventory", ts, ctx))

    for log in logs:
        log["shopdemo"] = {"memory": {"used_mb": used, "limit_mb": MEMORY_LIMIT_MB}}
    return logs


def service_logs(service: str, ts: datetime, ctx: dict, state: dict) -> list[dict]:
    """Logs one service writes when it handles a call. [] means the service is down."""
    scenario = state["scenario"]
    if service == "inventory":
        return inventory_memory_logs(ts, ctx, state)
    if scenario == "db_timeout" and service == "payment":
        override = db_timeout_logs(ts, ctx, state)
        if override:
            return override
    if scenario == "auth_cert_expired" and service == "auth":
        override = auth_cert_logs(ts, ctx, state)
        if override:
            return override
    return [make_normal_log(service, ts, ctx)]


# --------------------------------------------------------------------------
# Request flows
# --------------------------------------------------------------------------

def caller_failure_log(entry: str, dep: str, call_start: datetime, ctx: dict,
                       dep_log: dict | None) -> dict:
    """What the calling (entry) service logs when a dependency failed.

    call_start is when the entry service called the dependency."""
    if dep_log is None:
        ts = call_start + timedelta(milliseconds=5)
        msg = f"{dep}-service unavailable: connection refused"
        return add_error(base_log(entry, ts, ctx, "error", msg, random.randint(5, 20), 503),
                         "ServiceUnavailableError")

    dep_error = dep_log["error"]["type"]
    ts = call_start + timedelta(milliseconds=duration_ms_of(dep_log))
    if dep_error == "DatabaseTimeoutError":
        ts = call_start + timedelta(milliseconds=10000)  # caller's own client timeout
        msg = f"Call to {dep}-service timed out after 10000ms"
        return add_error(base_log(entry, ts, ctx, "error", msg, 10000, 504), "UpstreamTimeoutError")
    if dep_error == "CertificateExpiredError":
        msg = f"Token validation failed: {dep}-service returned 401 Unauthorized"
        return add_error(base_log(entry, ts, ctx, "error", msg, random.randint(20, 60), 401),
                         "AuthenticationError")
    if dep_error == "OutOfMemoryError":
        msg = f"{dep}-service unavailable: connection reset by peer"
        return add_error(base_log(entry, ts, ctx, "error", msg, random.randint(5, 20), 503),
                         "ServiceUnavailableError")

    # Normal business error downstream (4xx): request is aborted, not a system failure
    status = dep_log["http"]["response"]["status_code"]
    msg = f"Request aborted: {dep}-service returned {status} ({dep_error})"
    return base_log(entry, ts, ctx, "warn", msg, random.randint(50, 300), status)


def request_flow(ts: datetime, state: dict) -> list[dict]:
    """Simulate one incoming request; all logs share one trace.id."""
    entry = random.choices(list(ENTRY_WEIGHTS), weights=list(ENTRY_WEIGHTS.values()))[0]
    ctx = new_context()
    logs, t = [], ts

    for dep in CALL_CHAINS[entry]:
        dep_logs = service_logs(dep, t, ctx, state)
        logs.extend(dep_logs)
        last = dep_logs[-1] if dep_logs else None
        if last is None or is_failure(last):
            logs.append(caller_failure_log(entry, dep, t, ctx, last))
            return logs
        t += timedelta(milliseconds=duration_ms_of(last))

    logs.extend(service_logs(entry, t, ctx, state))
    return logs


def generate(scenario: str, hours: float, incident_minutes_ago: float, rate_per_min: float,
             now: datetime | None = None) -> list[dict]:
    """Backfill logs for the last `hours`, incident starting `incident_minutes_ago`."""
    now = now or datetime.now(timezone.utc)
    state = {
        "scenario": scenario,
        "incident_start": now - timedelta(minutes=incident_minutes_ago),
        "oom_logged": set(),
        "restart_logged": set(),
    }
    ts = now - timedelta(hours=hours)
    logs = []
    while ts < now:
        logs.extend(request_flow(ts, state))
        ts += timedelta(seconds=random.expovariate(rate_per_min / 60))  # Poisson arrivals
    return logs


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def send_to_elasticsearch(logs: list[dict]) -> None:
    try:
        from elasticsearch import Elasticsearch, helpers
    except ImportError:
        sys.exit("Missing package: pip install elasticsearch")

    password = os.getenv("ES_PASSWORD")
    if not password:
        sys.exit("Set ES_PASSWORD (and optionally ES_URL, ES_USER) as environment variables")

    es = Elasticsearch(os.getenv("ES_URL", "http://localhost:9200"),
                       basic_auth=(os.getenv("ES_USER", "elastic"), password))
    # Data streams only accept op_type "create" (append-only)
    actions = ({"_op_type": "create", "_index": DATA_STREAM, "_source": log} for log in logs)
    ok, errors = helpers.bulk(es, actions, chunk_size=1000, raise_on_error=False)
    print(f"Indexed {ok} documents into {DATA_STREAM}", file=sys.stderr)
    if errors:
        print(f"{len(errors)} errors, first: {errors[0]}", file=sys.stderr)


def print_summary(logs: list[dict]) -> None:
    counts = Counter((l["service"]["name"], l["log"]["level"]) for l in logs)
    print(f"\nGenerated {len(logs)} logs", file=sys.stderr)
    for (service, level), n in sorted(counts.items()):
        print(f"  {service:<10} {level:<9} {n}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="shopdemo ECS log generator")
    parser.add_argument("--scenario", choices=SCENARIOS, default="none")
    parser.add_argument("--hours", type=float, default=2, help="backfill window")
    parser.add_argument("--incident-minutes-ago", type=float, default=30)
    parser.add_argument("--rate", type=float, default=20, help="requests per minute")
    parser.add_argument("--output", choices=["stdout", "file", "es"], default="stdout")
    parser.add_argument("--file", default="logs.ndjson")
    parser.add_argument("--seed", type=int, help="random seed for reproducible runs")
    args = parser.parse_args()

    if args.incident_minutes_ago >= args.hours * 60:
        parser.error("--incident-minutes-ago must be inside the --hours window")
    if args.seed is not None:
        random.seed(args.seed)

    logs = generate(args.scenario, args.hours, args.incident_minutes_ago, args.rate)
    logs.sort(key=lambda l: l["@timestamp"])

    if args.output == "stdout":
        for log in logs:
            print(json.dumps(log))
    elif args.output == "file":
        with open(args.file, "w", encoding="utf-8") as f:
            for log in logs:
                f.write(json.dumps(log) + "\n")
        print(f"Wrote {args.file}", file=sys.stderr)
    else:
        send_to_elasticsearch(logs)

    print_summary(logs)


if __name__ == "__main__":
    main()