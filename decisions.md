# Architecture Decision Records (ADRs) — URL Shortener Platform

This document tracks technical design choices, architectural trade-offs, and failure mode mitigations for the URL Shortener platform.

---

## ADR-001: Splitting Liveness (`/health/live`) and Readiness (`/health/ready`) Probes

### Status
Accepted

### Context & Problem
In production containerized deployments (Kubernetes, AWS ECS, Docker Compose) behind load balancers (AWS ALB, Nginx):
1. Conflating "Is the process alive?" with "Can this instance serve customer traffic?" causes severe service outages.
2. If a database or Redis connection pool experiences a transient connection reset, an un-split health endpoint returns an error.
3. If an orchestrator uses a single health endpoint for both container lifecycle and load-balancer routing, it will **kill and restart the application container** when the database is temporarily unreachable. Restarting all app containers simultaneously during a transient DB blip creates a cascading thundering-herd outage.

### Decision
We split health monitoring into two distinct endpoints:

1. **`GET /health/live` (Liveness Probe)**:
   - **Purpose:** Answers *"Is the Python/Uvicorn process running and responding to HTTP requests?"*
   - **Behavior:** Returns `200 OK` `{"status": "alive"}` immediately without querying DB or Redis.
   - **Orchestrator Action on Failure:** Restart container.

2. **`GET /health/ready` (Readiness Probe)**:
   - **Purpose:** Answers *"Is this specific instance able to serve real traffic right now?"*
   - **Behavior:** Executes `SELECT 1` on SQLite/Postgres DB and `PING` on Redis. Returns `200 OK` if all dependencies respond, or `503 Service Unavailable` if any dependency fails.
   - **Orchestrator Action on Failure:** Temporarily remove instance from load balancer traffic rotation until dependencies recover, **without restarting the container**.

3. **`GET /health` (Backwards Compatibility Alias)**:
   - Legacy endpoint aliased to `/health/ready` to ensure existing monitoring agents continue operating without breaking changes.

---

## ADR-002: Request/Response Logging Middleware & Zero-Trust PII/Secret Redaction

### Status
Accepted

### Context & Problem
Logging incoming HTTP requests and responses is necessary for debugging production issues and calculating API latency. However, naive request logging poses a severe security and compliance vulnerability (OWASP A09):
1. User passwords (e.g., `/auth/login`, `/auth/signup`), JWT access tokens (`Authorization: Bearer <jwt>`), and secret developer keys (`X-API-Key: sk_live_...`) can leak in plain text into centralized logging infrastructure (Datadog, AWS CloudWatch, ELK).
2. Once secrets land in log aggregators, they become accessible to non-security team members, compliance auditors, and third-party log retention vendors.

### Decision
We implemented a Starlette `BaseHTTPMiddleware` (`app/middleware.py`) that executes zero-trust, pre-log redaction on every request and response:

1. **Header Redaction:**
   - Case-insensitive denylist (`authorization`, `x-api-key`, `cookie`, `set-cookie`) is checked before writing header dicts to logs. Matched headers are replaced with `[REDACTED]`.
2. **Recursive JSON Body Redaction:**
   - JSON request payloads are parsed and recursively traversed. Any field matching sensitive keys (`password`, `access_token`, `refresh_token`, `plain_key`, `secret`, `api_key`, `token`) is mutated to `[REDACTED]` prior to serialization.
3. **High-Precision Latency Measurement:**
   - Uses `time.perf_counter()` to measure total processing time and appends an `X-Process-Time-Ms` HTTP header to all responses.

### Rationale & Trade-offs
- **Denylist vs. Allowlist:** We chose a targeted denylist for common security tokens combined with recursive payload parsing. This avoids discarding useful operational context while guaranteeing sensitive keys are sanitized.
- **Starlette Body Stream Re-injection:** Request body streams in Starlette can only be read once. The middleware clones and re-injects the body stream via a custom `receive()` async callable so downstream FastAPI route handlers receive intact request bodies.

---

## ADR-003: API Versioning Strategy (`/v1/` vs `/v2/`) & RFC 8594 Deprecation Policy

### Status
Accepted

### Context & Problem
As an API evolves, changing response schemas or renaming JSON keys breaks existing production integrations (mobile apps, enterprise backend integrations, external SDKs).
1. We needed a clean URL-path versioning strategy (`/v1/`, `/v2/`) so legacy clients remain unaffected when new breaking features launch.
2. We needed a standardized, machine-readable mechanism to signal to clients when an older API version is being deprecated before eventual shutdown.

### Decision
1. **URL-Path Prefixing (`/v1/` and `/v2/`):**
   - All legacy endpoints are grouped under `/v1/` using FastAPI `APIRouter(prefix="/v1")`.
   - Breaking API shape changes are introduced under `/v2/` (e.g. `POST /v2/shorten` nests fields inside `{"data": {...}, "api_version": "v2"}`).
   - Un-prefixed routes (e.g. `POST /shorten`) are preserved for backwards compatibility.

2. **RFC 8594 Sunset & Deprecation Response Headers (`V1DeprecationMiddleware`):**
   - Middleware intercepts all responses starting with `/v1/` and automatically attaches:
     - `Sunset: Wed, 31 Dec 2026 23:59:59 GMT` (Official RFC 8594 Sunset Header indicating hard end-of-life timestamp).
     - `X-API-Deprecated: true` (Machine-readable warning flag).
     - `X-API-Migration-Doc: /docs#v2-migration` (Link to migration documentation).

### Rationale & Trade-offs
- **URL-Path Versioning vs. Header Versioning (`Accept: application/vnd.company.v1+json`):** URL-path versioning was chosen because it is transparent, cacheable by CDN proxies (Cloudflare, AWS CloudFront) without complex `Vary: Accept` headers, and easy to inspect in API docs.
- **Graceful Lifecycle Management:** Automated `Sunset` headers allow DevOps and external developers to monitor API deprecation dates via standard HTTP client telemetry.

---

## ADR-004: Third-Party OAuth 2.0 Integration & Account Linking Policy

### Status
Accepted — **state handling revised by ADR-010** (the in-memory `OAUTH_STATE_STORE`
described below was replaced by Redis-backed single-use keys).

### Context & Problem
While self-contained auth (JWT + hashed passwords) works for standard credential logins, enterprise SaaS applications require federated identity verification (e.g. "Sign in with Google"). OAuth 2.0 authorization code flow introduces specific security risks:
1. **Cross-Site Request Forgery (CSRF) on Callback:** Attackers can trick a victim's browser into executing an OAuth callback using the attacker's authorization code, binding the victim's account to the attacker's third-party identity.
2. **Account Linking Collision:** What happens when a user signs up locally with `user@gmail.com` using a password, and later clicks "Sign in with Google" using the same verified `user@gmail.com` email address?

### Decision
1. **OAuth 2.0 Authorization Code Flow & CSRF State Protection:**
   - On `/auth/google/login`, the server generates a 32-byte cryptographically secure random `state` token using `secrets.token_urlsafe(32)` and stores it in single-use memory (`OAUTH_STATE_STORE`).
   - On `/auth/google/callback`, the server enforces `verify_and_consume_oauth_state(state)`. If the `state` parameter is missing or mismatched, request is immediately rejected with HTTP `400 Bad Request`.

2. **Automated Account Linking Strategy:**
   - **Step 1 (Google Sub Match):** Check database for `UserModel.google_sub == sub`. If found, return existing user.
   - **Step 2 (Verified Email Linking):** If no `google_sub` match exists, check database for `UserModel.email == email`. If a local user exists with that verified email, automatically link `google_sub` to the user record without creating duplicate accounts.
   - **Step 3 (New OAuth Registration):** If neither match exists, create a new user record with `password_hash = None` and auto-generated unique username.

3. **Unified Session Issuance:**
   - Upon successful OAuth exchange and account linking, the server issues a standard platform **HS256 JWT Access Token**, ensuring downstream API routes behave identically regardless of login provider.

---

## ADR-005: High-Throughput Redis Atomic Click Counter Buffer

### Status
**Superseded by ADR-007.** The `clicks_buffer:*` INCR helpers were removed from
`app/cache.py`; a counter with no defined flush owner loses data whenever Redis is
evicted or restarted. The queue-and-worker pipeline replaces it.

### Context & Problem
In high-scale URL shorteners receiving thousands of clicks per second on viral links, executing a synchronous SQL database update (`UPDATE urls SET clicks = clicks + 1 WHERE short_code = 'xyz'`) on every single HTTP redirect creates severe database row locks, disk I/O bottlenecks, and degrades redirect response times from < 2ms to over 50ms.

### Decision
We implemented a high-performance Redis in-memory click counter buffer (`increment_click_buffer(short_code)` in `app/cache.py`):
1. **0.1ms Atomic Increment:** On `GET /{short_code}`, the server executes an atomic Redis `INCR clicks_buffer:{short_code}` in memory.
2. **Instant Redirect Execution:** The server returns `307 Temporary Redirect` immediately without waiting for disk I/O.
3. **Synchronous DB Syncing:** Accumulated counts in Redis buffer are committed to SQL database storage, keeping analytics up-to-date while maintaining sub-2ms redirect latencies.

---

## ADR-006: Custom Vanity Aliases & Link Expiration Policy (HTTP 410)

### Status
Accepted

### Context & Problem
1. **Vanity Aliases:** Enterprise users require branded custom short URLs (e.g. `http://localhost:8000/my-brand`) instead of random Base62 hashes. However, allowing arbitrary custom aliases introduces system route hijacking risks (e.g. creating alias `dashboard` or `health`).
2. **Link Expiration:** Temporary marketing links require lifecycle boundaries. Serving expired links indefinetely wastes bandwidth and creates stale traffic.

### Decision
1. **Reserved Keyword Protection & Regex Validation:**
   - Custom aliases must match regex `^[a-zA-Z0-9_-]{3,50}$`.
   - A strict denylist (`RESERVED_ALIASES`) protects system paths (`dashboard`, `docs`, `health`, `v1`, `v2`, `auth`, `openapi.json`, `static`, `urls`, `login`, `signup`).
2. **HTTP 410 Gone Expiration Enforcement:**
   - Short URLs support an optional ISO-8601 UTC timestamp `expires_at`.
   - When accessed after `expires_at`, `GET /{short_code}` returns **HTTP 410 Gone** (`{"error": {"code": 410, "message": "This short link has expired."}}`).





---

## ADR-007: Click Analytics via Redis Queue + Worker with Dead-Letter Queue

### Status
Accepted (supersedes ADR-005)

### Context & Problem
Counting a click is a write, and writes on the redirect path make the user wait for
disk I/O and row locks. ADR-005 moved the counter into a Redis `INCR`, which removed
the latency but created a durability hole: nothing owned flushing the buffer to SQL, so
an eviction or restart silently discarded analytics. "Fast but occasionally wrong" is
the worst outcome for billing-adjacent data.

### Decision
1. **Enqueue, don't count.** `GET /{short_code}` `RPUSH`es `{short_code, timestamp}` onto
   `click_events_queue` and returns. The redirect never issues a SQL write.
2. **A worker owns persistence.** `app/worker.py` `BLPOP`s events and applies them via
   `crud.increment_clicks`, so the DB write happens off the request path.
3. **Bounded retries, then a DLQ.** A failed write is requeued with an incremented
   `retry_count` and exponential backoff; after `MAX_RETRIES` the event moves to
   `click_events_dlq` instead of cycling forever. `handle_click_event()` returns the
   outcome (`ok` / `retried` / `dlq` / `skipped`) so the policy is unit-testable without
   running the blocking loop.

### Rationale & Trade-offs
Click counts become **eventually consistent** — a dashboard can lag the true count by
the worker's drain time. That is an acceptable trade for a metric, and it is a trade we
make explicitly rather than by accident. The queue is still Redis, so a Redis loss can
drop in-flight events; durable delivery would need Streams with consumer groups, which
is the natural next step if analytics ever become billable.

---

## ADR-008: Cache Entries Carry the Link's Lifecycle

### Status
Accepted

### Context & Problem
The redirect answers from Redis without reading SQL, so any rule enforced only in the
database is invisible to the hot path. Two rules were: expiry (`HTTP 410`) and soft
delete (`HTTP 404`). Both were checked on the cache-miss path only, so a cached link
kept returning `307` after it expired or was deleted — for up to the full 2-hour TTL.
Integration tests missed it because the test rig stubs the cache to always miss, so no
test ever exercised the path that production uses for nearly every request.

### Decision
1. **TTL is clamped to the deadline:** `set_cached_url()` caches for
   `min(2h, seconds_until_expires_at)`, and refuses to cache an already-expired link.
   Redis evicts the key at the deadline, the next request falls to the DB path, and the
   correct `410` is returned — with no extra read on the hot path.
2. **Soft delete evicts:** `DELETE /urls/{code}` calls `delete_cached_url()`, so a
   deleted link stops redirecting immediately rather than at TTL expiry.

### Rationale & Trade-offs
Encoding the lifecycle in the TTL keeps the hot path at one Redis read; the alternative
(storing an expiry alongside the URL and comparing on every request) costs a JSON
decode per redirect to solve a problem Redis already solves. Trade-off: expiry is
enforced at second granularity by Redis eviction, not to the millisecond.

---

## ADR-009: Rate Limiter Fails Open, and Costs One Round-Trip

### Status
Accepted

### Context & Problem
Benchmarking the redirect path (`scripts/bench.py`) showed **six sequential Redis
round-trips per redirect**, four of them from the sliding-window limiter, at a measured
server-side p50 of 10.6 ms. Separately, every one of those calls was unguarded: an
unreachable Redis raised, so a cache outage produced `500`s on the one route that must
survive — even though the database could still answer.

### Decision
1. **One `MULTI` per check:** prune, record, count, and refresh TTL ship as a single
   pipeline. Measured effect: server p50 10.6 ms → 5.8 ms, throughput at `c=50`
   127 → 222 req/s.
2. **Fail open on outage:** if the pipeline raises, the request is allowed and a warning
   is logged. Availability of redirects outranks enforcement of a soft quota.
3. **The quota is configuration:** `IP_RATE_LIMIT` moved into `Settings`, so load tests
   and staging can raise it without a code change.

4. **Bounded client timeouts, no retries.** Catching exceptions is not enough: with
   redis-py's defaults an unreachable Redis does not raise promptly, it *blocks* —
   measured at ~8.7s per call from backoff retries, and an indefinite hang across a
   full redirect. The client now uses a 0.25s connect/read timeout
   (`REDIS_TIMEOUT_SECONDS`) and `Retry(NoBackoff(), 0)`, which is what turns the
   `except` branches below into an actual fallback. Verified against a dead Redis port:
   redirects return `307` in ~2.3s instead of hanging.

### Rationale & Trade-offs
Failing open means a Redis outage temporarily removes abuse protection; the alternative,
failing closed, converts a degraded cache into a total outage. The warning log is the
alerting hook. The hit is recorded *before* it is counted, so a rejected request still
consumes a slot and a flood earns no free retries — at the cost of counting requests we
ultimately rejected.

**Known ceiling:** while Redis is down every request still pays ~4 failed connection
attempts (~2.3s). A circuit breaker that trips after N consecutive failures and skips
Redis entirely would restore normal latency; it is deliberately not built yet, since
the readiness probe already removes the instance from rotation during an outage.

---

## ADR-010: OAuth CSRF State Is Redis-Backed and Single-Use

### Status
Accepted (revises ADR-004)

### Context & Problem
State validation kept tokens in a module-level dict and ended with a fallback:
`return len(state) >= 20`. Any sufficiently long attacker-supplied string therefore
passed validation, which is equivalent to having no CSRF protection on the OAuth
callback at all. The in-process dict was also incorrect under more than one worker and
across restarts — which is precisely why the unsafe fallback had been added.

### Decision
State tokens are stored as `oauth_state:{token}` keys in Redis with a 15-minute TTL and
consumed with `DELETE`, whose return value (number of keys removed) makes consumption
atomic. Only the first caller of a given state sees `1`; replays and forgeries see `0`
and are rejected. There is no shape-based fallback — unknown state fails closed.

### Rationale & Trade-offs
Correctness across workers now comes from shared state rather than from weakening the
check. Redis becomes a hard dependency for *login* (unlike redirects, which degrade
gracefully) — the right call, since failing open on a CSRF check is not a trade worth
making.
