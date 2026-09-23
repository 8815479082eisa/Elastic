# Log Schema – shopdemo

## Target
- Data stream: `logs-shopdemo-default`
  - `data_stream.type`: `logs`
  - `data_stream.dataset`: `shopdemo`
  - `data_stream.namespace`: `default`
- Standard: Elastic Common Schema (ECS)
- All timestamps in UTC (ISO 8601)

## Core fields
| Field | Type | Example | Why needed |
|---|---|---|---|
| @timestamp | date | 2026-09-23T14:02:11.482Z | Time axis for all queries and aggregations |
| log.level | keyword | error | Filter by severity (info, warn, error, critical) |
| service.name | keyword | payment | Group and filter errors per service |
| service.version | keyword | 1.4.2 | Correlate incidents with deployments |
| host.name | keyword | payment-01 | Identify the affected instance |
| message | match_only_text | Payment authorized | Human-readable log line, full-text search |
| trace.id | keyword | 4bf92f3577b34da6a3ce929d0e0e4736 | Follow one request across services |
| event.duration | long (ns) | 184000000 | Latency analysis |
| http.request.method | keyword | POST | Request context |
| url.path | keyword | /api/payment/charge | Endpoint that failed |
| http.response.status_code | long | 504 | Distinguish timeouts, auth errors, unavailability |
| error.type | keyword | DatabaseTimeoutError | Exact grouping of errors (aggregation) |
| error.message | match_only_text | Timeout acquiring connection... | Error details for the agent and RAG search |

## PII fields (for DSGVO masking in phase 4)
| Field | Type | Example | Note |
|---|---|---|---|
| user.email | keyword | anna.mueller@example.de | Must be masked before sending to an LLM |
| source.ip | ip | 192.168.10.45 | Personal data under DSGVO, must be masked |

## Custom fields
ECS has no field for application memory usage, so a custom namespace is used
(ECS recommendation: custom fields must not collide with ECS field names).

| Field | Type | Example | Why needed |
|---|---|---|---|
| shopdemo.memory.used_mb | long | 1650 | Detect memory leak trend in inventory |
| shopdemo.memory.limit_mb | long | 2048 | Context for used_mb |

## Normal traffic per service
Baseline: ~95% info, ~4% warn, ~1% error (no system is error-free).

| Service | Typical INFO message | Typical WARN message | Background ERROR |
|---|---|---|---|
| checkout | Order created (order_id=...) | Cart validation slow (820ms) | InvalidCartError: item no longer available |
| payment | Payment authorized (amount=...) | Payment provider response slow (1.2s) | CardDeclinedError: card declined by issuer |
| auth | Token issued for user | Login rate high for source IP | InvalidCredentialsError: wrong password |
| inventory | Stock reserved (sku=...) | Low stock for sku=... | StockNotFoundError: unknown sku |

## Incident scenarios

### 1. db_timeout (cascading failure)
| Order | Service | log.level | error.type | error.message / message | status |
|---|---|---|---|---|---|
| 1 | payment | warn | – | DB connection pool usage high (45/50) | – |
| 2 | payment | error | DatabaseTimeoutError | Timeout acquiring connection from pool payment-db (active=50/50, waited 5000ms) | 500 |
| 3 | checkout | error | UpstreamTimeoutError | Call to payment-service timed out after 10000ms | 504 |

- Root cause: payment database connection pool exhausted
- Checkout errors share `trace.id` with payment errors (evidence of cascade)

### 2. auth_cert_expired (simultaneous failure)
| Order | Service | log.level | error.type | error.message / message | status |
|---|---|---|---|---|---|
| 0 (days before) | auth | warn | – | TLS certificate expires in 3 days (CN=auth.shopdemo.internal) | – |
| 1 | auth | error | CertificateExpiredError | TLS handshake failed: certificate has expired (CN=auth.shopdemo.internal) | 500 |
| 2 | checkout, payment, inventory | error | AuthenticationError | Token validation failed: auth-service returned 401 Unauthorized | 401 |

- Root cause: expired TLS certificate on auth-service
- All services fail at the same moment (time correlation, not a slow cascade)

### 3. memory_leak (gradual degradation)
| Order | Service | log.level | error.type | error.message / message | status |
|---|---|---|---|---|---|
| 1 | inventory | warn | – | Memory usage at 75% (used_mb rising) | – |
| 2 | inventory | warn | – | Long GC pause detected (1.8s), response time increasing | – |
| 3 | inventory | critical | OutOfMemoryError | Process killed: out of memory (used=2048MB, limit=2048MB) | – |
| 4 | checkout | error | ServiceUnavailableError | inventory-service unavailable: connection refused | 503 |
| 5 | inventory | info | – | Service restarted (memory reset) | – |

- Root cause: memory leak in inventory, visible as rising `shopdemo.memory.used_mb`
- Pattern repeats after restart if not fixed

## Design decisions
- No field reveals the injected scenario name (e.g. no `labels.scenario`).
  The agent must find the root cause from the log data itself, like in production.
- `trace.id` enables exact correlation; time-based correlation is the fallback.
- `keyword` for fields used in filters and aggregations,
  `match_only_text` for free text (space-efficient, ECS default for logs).