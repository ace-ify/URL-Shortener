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
Accepted

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
Accepted

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




