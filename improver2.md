# URL Shortener — Round 2 Improvements (Post-Deadline Backlog)

## Status
Not urgent — this is explicitly a **post-Aug-30** backlog, not something to build before the application deadline. The core fundamentals rebuild (dual auth, full CRUD, RBAC, migrations, centralized exceptions, collision-safe generation, background job queue, tests, CI) is already complete and confirmed working. This document only covers the next round of additions, to be picked up later as ongoing learning alongside the job search.

## Why these specific items landed here
Each item below was identified as a genuine backend fundamentals gap not covered by any of the 5 main portfolio projects, and was judged to fit naturally onto the URL Shortener specifically — because it already has working auth, a real API surface, and CI in place, making it the cheapest place to bolt each one on without inventing new project scope.

---

## 1. Third-party OAuth login (Sign in with Google)

### Why this is a distinct skill from what's already built
The existing auth system (JWT + API keys) is entirely self-contained — you issue and verify your own tokens. OAuth introduces a fundamentally different mechanic: **delegating identity verification to a third party** and handling the authorization-code exchange flow, which has its own failure modes (state parameter for CSRF protection, token exchange, handling a user who signs up via Google but later also has a local password).

### What to build
- Add "Sign in with Google" as a third auth path alongside existing signup/login
- Implement the standard OAuth2 authorization code flow: redirect to Google's consent screen → receive an authorization code → exchange it server-side for tokens → fetch the user's profile → create or link a local user record
- Handle account linking: what happens if someone signs up locally with `user@gmail.com`, then later tries "Sign in with Google" using the same email? Decide and document this explicitly (auto-link vs. reject vs. prompt)
- Use and validate the `state` parameter to prevent CSRF on the OAuth callback

### ADR to write
- [ ] How local accounts and OAuth accounts are linked/merged, and why

---

## 2. API versioning strategy

### Why this is a distinct skill
Owning your own API end-to-end from day one means you never face the real problem: **changing behavior for new clients without breaking existing ones.** This only becomes a real design question once you deliberately introduce a breaking change and have to support both the old and new shape at once.

### What to build
- Introduce `/v1/` prefix on all existing routes (moving current unprefixed routes under it, with redirects or deprecation warnings for the old paths)
- Deliberately make one breaking change and ship it as `/v2/` for a single endpoint (e.g. change the shape of `URLShortenResponse` to nest fields differently) while `/v1/` keeps working unchanged
- Add a deprecation header (e.g. `Sunset` or a custom `X-API-Deprecated` header) on `/v1/` responses once `/v2/` exists, so clients get a machine-readable signal before the old version is eventually removed

### ADR to write
- [ ] URL-path versioning (`/v1/`) vs. header-based versioning — why you chose one
- [ ] Deprecation policy: how long `/v1/` stays supported after `/v2/` ships, and how that's communicated

---

## 3. Health check vs. readiness check

### Why this is a distinct skill
"Is the process running" and "is this instance actually able to serve real traffic right now" are genuinely different questions, and conflating them is a common real-world outage cause (a load balancer keeps routing traffic to an instance that's alive but whose DB connection pool died).

### What to build
- `GET /health/live` — trivial check, just confirms the process is up and responding at all
- `GET /health/ready` — checks actual dependencies: can it reach Postgres (a real `SELECT 1`), can it reach Redis (a real `PING`)? Returns 503 if any dependency is down, even though the process itself is alive
- Document in the README which endpoint a load balancer or container orchestrator should point at for which purpose

### ADR to write
- [ ] Why liveness and readiness are split into two separate endpoints instead of one

---

## 4. Request/response logging middleware with PII redaction

### Why this is a distinct skill
Structured logging already exists via the centralized exception handlers, but full request/response logging for debugging is a different, broader concern — and the real skill here isn't "log everything," it's **knowing what never to log.** Passwords, JWTs, and API keys ending up in plaintext logs is a real, common security mistake.

### What to build
- Middleware that logs every request: method, path, status code, latency, and a truncated/redacted body
- Explicit redaction rules: `password`, `access_token`, `X-API-Key` header, and any `Authorization` header value must never appear in logs, even in a truncated request body — replace with `[REDACTED]` before logging, not after
- Test this directly: write a test that sends a request containing a password field and asserts the captured log line does not contain the raw value

### ADR to write
- [ ] What fields are redacted and how the redaction is enforced (allowlist of loggable fields vs. denylist of redacted ones — denylist is riskier, since it's easy to forget to add a new sensitive field; consider documenting why you picked one approach)

---

## Suggested order to tackle these
Health/readiness checks first (cheapest, ~30 minutes, no new concepts). Then request logging + redaction (moderate, reinforces security thinking). Then API versioning (requires actually restructuring routes, a bit more involved). OAuth last — it's the most conceptually different from anything already built, and benefits from not being rushed.

## Note
The other 14 items from the full gap list (bulk operations, outgoing webhooks, N+1 queries, file uploads, etc.) each have a different, better-fitting home — see the broader backlog discussion. This document intentionally covers only the four that belong on the URL Shortener specifically.