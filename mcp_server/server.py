"""MCP server exposing Elasticsearch observability tools for the shopdemo system."""

from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

import queries

Service = Literal["checkout", "payment", "auth", "inventory"]
Level = Literal["info", "warn", "error", "critical"]

TimeStart = Annotated[
    str,
    Field(description="Start of time range. ES date math like 'now-1h', 'now-30m', "
                      "or ISO 8601 UTC like '2026-09-23T10:00:00Z'."),
]
TimeEnd = Annotated[str, Field(description="End of time range, 'now' or ISO 8601 UTC.")]

mcp = FastMCP(
    name="elastic-observability",
    instructions=(
        "Tools for incident analysis on the shopdemo system (services: checkout, payment, "
        "auth, inventory). Logs are stored in Elasticsearch in ECS format. "
        "Recommended workflow: 1) get_error_stats to see which services and error types "
        "are abnormal and when errors started, 2) find_correlated_errors to separate root "
        "cause from downstream effects, 3) search_logs to read example log lines. "
        "4) Check for early warning signs: search warn logs of the root-cause service in "
        "the period before the incident started, since warnings are often rare but important. "
        "Some errors are normal background noise (a few per hour); focus on sudden spikes. "
        "All timestamps are UTC."
    ),
)


@mcp.tool
def search_logs(
    service: Annotated[Service | None, Field(description="Filter by service name.")] = None,
    level: Annotated[Level | None, Field(description="Filter by log level.")] = None,
    start: TimeStart = "now-1h",
    end: TimeEnd = "now",
    query: Annotated[str | None, Field(description="Free-text search in message and error message.")] = None,
    size: Annotated[int, Field(ge=1, le=50, description="Number of log lines to return (max 50).")] = 20,
) -> dict:
    """Search individual log lines, newest first.

    Use this to read concrete examples after you know which service and time range matter.
    Returns total_matches (all matching logs) and a small sample of compact log lines.
    """
    return queries.search_logs(service, level, start, end, query, size)


@mcp.tool
def get_error_stats(
    service: Annotated[Service | None, Field(description="Limit to one service.")] = None,
    start: TimeStart = "now-1h",
    end: TimeEnd = "now",
    interval: Annotated[str, Field(description="Histogram bucket size, e.g. '1m', '5m', '1h'. "
                                               "Use larger buckets for longer ranges.")] = "5m",
) -> dict:
    """Count error and critical logs per service, per error type, and over time.

    Start here to find which service is abnormal and when the problem began.
    Counting is done in Elasticsearch, so this is cheap even for large time ranges.
    """
    return queries.get_error_stats(service, start, end, interval)


@mcp.tool
def find_correlated_errors(
    start: TimeStart = "now-1h",
    end: TimeEnd = "now",
) -> dict:
    """Find how errors in different services relate to each other.

    Returns a timeline (first/last occurrence of each service + error type, sorted by
    first occurrence) and error chains: requests whose errors span several services,
    linked by trace.id, in chronological order. The first element of a frequent chain
    is the likely root cause; later elements are downstream effects.
    """
    return queries.find_correlated_errors(start, end)


if __name__ == "__main__":
    mcp.run()  # stdio transport, used by Claude Desktop