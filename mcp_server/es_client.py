"""Shared Elasticsearch connection for the MCP server."""

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from elasticsearch import Elasticsearch

# Load .env from the project root, independent of the current working directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

LOGS_INDEX = "logs-shopdemo-*"


@lru_cache(maxsize=1)
def get_client() -> Elasticsearch:
    """Return one shared Elasticsearch client (created on first use)."""
    url = os.getenv("ES_URL", "http://localhost:9200")
    user = os.getenv("ES_USER", "elastic")
    password = os.getenv("ELASTIC_PASSWORD")
    if not password:
        raise RuntimeError("ELASTIC_PASSWORD is not set in .env")
    return Elasticsearch(url, basic_auth=(user, password), request_timeout=30)


if __name__ == "__main__":
    info = get_client().info()
    print(f"Connected to cluster '{info['cluster_name']}', version {info['version']['number']}")