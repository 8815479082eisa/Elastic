"""Elasticsearch queries used by the MCP tools. Plain functions, testable without MCP."""

import json
from collections import Counter

from es_client import LOGS_INDEX, get_client

# Only fields that help incident analysis (PII like user.email / source.ip excluded)
LOG_FIELDS = [
    "@timestamp",
    "service.name",
    "log.level",
    "message",
    "error.type",
    "http.response.status_code",
    "trace.id",
]
MAX_SIZE = 50
ERROR_LEVELS = ["error", "critical"]

# Returned instead of crashing when no index matches (e.g. data stream deleted)
NO_DATA_NOTE = "No logs found for this time range (data stream empty or missing)."


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _time_filter(start: str, end: str) -> dict:
    """Range filter on @timestamp. Accepts ISO timestamps or ES date math like 'now-1h'."""
    return {"range": {"@timestamp": {"gte": start, "lte": end}}}


def _flatten(source: dict) -> dict:
    """Turn a nested ECS document into a compact flat dict for the LLM."""
    return {
        "timestamp": source.get("@timestamp"),
        "service": source.get("service", {}).get("name"),
        "level": source.get("log", {}).get("level"),
        "message": source.get("message"),
        "error_type": source.get("error", {}).get("type"),
        "status": source.get("http", {}).get("response", {}).get("status_code"),
        "trace_id": source.get("trace", {}).get("id"),
    }


# --------------------------------------------------------------------------
# Tool 1: search_logs
# --------------------------------------------------------------------------

def search_logs(
    service: str | None = None,
    level: str | None = None,
    start: str = "now-1h",
    end: str = "now",
    query: str | None = None,
    size: int = 20,
) -> dict:
    """Search logs with optional filters. Newest first."""
    filters = [_time_filter(start, end)]
    if service:
        filters.append({"term": {"service.name": service}})
    if level:
        filters.append({"term": {"log.level": level}})

    must = []
    if query:
        must.append({"multi_match": {"query": query, "fields": ["message", "error.message"]}})

    size = max(1, min(size, MAX_SIZE))
    resp = get_client().search(
        index=LOGS_INDEX,
        query={"bool": {"filter": filters, "must": must}},
        sort=[{"@timestamp": "desc"}],
        size=size,
        source=LOG_FIELDS,
        track_total_hits=True,
    )

    hits = resp["hits"]["hits"]
    return {
        "total_matches": resp["hits"]["total"]["value"],
        "returned": len(hits),
        "logs": [_flatten(h["_source"]) for h in hits],
    }


# --------------------------------------------------------------------------
# Tool 2: get_error_stats
# --------------------------------------------------------------------------

def get_error_stats(
    service: str | None = None,
    start: str = "now-1h",
    end: str = "now",
    interval: str = "5m",
) -> dict:
    """Aggregate error and critical logs: per service, per error type, and over time."""
    filters = [
        _time_filter(start, end),
        {"terms": {"log.level": ERROR_LEVELS}},
    ]
    if service:
        filters.append({"term": {"service.name": service}})

    resp = get_client().search(
        index=LOGS_INDEX,
        query={"bool": {"filter": filters}},
        size=0,  # no documents, only aggregations
        aggs={
            "by_service": {
                "terms": {"field": "service.name", "size": 10},
                "aggs": {"by_error_type": {"terms": {"field": "error.type", "size": 5}}},
            },
            "over_time": {
                "date_histogram": {
                    "field": "@timestamp",
                    "fixed_interval": interval,
                    "min_doc_count": 1,
                }
            },
        },
        track_total_hits=True,
    )

    aggs = resp.get("aggregations")
    if not aggs:
        return {"total_errors": 0, "note": NO_DATA_NOTE}

    return {
        "total_errors": resp["hits"]["total"]["value"],
        "by_service": [
            {
                "service": b["key"],
                "errors": b["doc_count"],
                "error_types": {t["key"]: t["doc_count"] for t in b["by_error_type"]["buckets"]},
            }
            for b in aggs["by_service"]["buckets"]
        ],
        "over_time": [
            {"time": b["key_as_string"], "errors": b["doc_count"]}
            for b in aggs["over_time"]["buckets"]
        ],
    }


# --------------------------------------------------------------------------
# Tool 3: find_correlated_errors
# --------------------------------------------------------------------------

def find_correlated_errors(start: str = "now-1h", end: str = "now", max_traces: int = 500) -> dict:
    """Find how errors relate: timeline per service/error type, and error chains via shared trace.id."""
    error_query = {
        "bool": {
            "filter": [
                _time_filter(start, end),
                {"terms": {"log.level": ERROR_LEVELS}},
            ]
        }
    }

    resp = get_client().search(
        index=LOGS_INDEX,
        query=error_query,
        size=0,
        aggs={
            # 1. When did each (service, error type) start and stop?
            "by_service": {
                "terms": {"field": "service.name", "size": 10},
                "aggs": {
                    "by_error_type": {
                        "terms": {"field": "error.type", "size": 10},
                        "aggs": {
                            "first_seen": {"min": {"field": "@timestamp"}},
                            "last_seen": {"max": {"field": "@timestamp"}},
                        },
                    }
                },
            },
            # 2. Requests (trace.id) with errors in more than one service
            "traces": {
                "terms": {"field": "trace.id", "size": max_traces, "min_doc_count": 2},
                "aggs": {
                    "services": {"cardinality": {"field": "service.name"}},
                    "chain": {
                        "top_hits": {
                            "size": 5,
                            "sort": [{"@timestamp": "asc"}],
                            "_source": ["service.name", "error.type"],
                        }
                    },
                },
            },
        },
    )
    aggs = resp.get("aggregations")
    if not aggs:
        return {"error_timeline": [], "multi_service_error_traces": 0, "error_chains": [],
                "note": NO_DATA_NOTE}

    timeline = [
        {
            "service": s["key"],
            "error_type": t["key"],
            "count": t["doc_count"],
            "first_seen": t["first_seen"]["value_as_string"],
            "last_seen": t["last_seen"]["value_as_string"],
        }
        for s in aggs["by_service"]["buckets"]
        for t in s["by_error_type"]["buckets"]
    ]
    timeline.sort(key=lambda x: x["first_seen"])

    chains, examples = Counter(), {}
    for bucket in aggs["traces"]["buckets"]:
        if bucket["services"]["value"] < 2:
            continue
        steps = [
            f'{h["_source"]["service"]["name"]}:{h["_source"].get("error", {}).get("type")}'
            for h in bucket["chain"]["hits"]["hits"]
        ]
        chain = " -> ".join(steps)
        chains[chain] += 1
        examples.setdefault(chain, bucket["key"])

    return {
        "error_timeline": timeline,
        "multi_service_error_traces": sum(chains.values()),
        "error_chains": [
            {"chain": c, "occurrences": n, "example_trace_id": examples[c]}
            for c, n in chains.most_common(5)
        ],
    }


# --------------------------------------------------------------------------
# Manual test (not used by the MCP server)
# --------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== search_logs ===")
    print(json.dumps(search_logs(service="payment", level="error", start="now-24h", size=3), indent=2))

    print("\n=== get_error_stats ===")
    print(json.dumps(get_error_stats(start="now-24h", interval="10m"), indent=2))

    print("\n=== find_correlated_errors ===")
    print(json.dumps(find_correlated_errors(start="now-24h"), indent=2))