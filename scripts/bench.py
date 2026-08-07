"""
Redirect hot-path benchmark.

Measures what the README claims: redirect throughput and the latency gap between a
Redis cache hit and the database fallback. Uses only httpx (already a dependency), so
there is nothing extra to install.

Run the server with the per-IP limiter raised, or the benchmark just measures 429s:

    IP_RATE_LIMIT=1000000 venv/Scripts/python -m uvicorn app.main:app --port 8013
    venv/Scripts/python scripts/bench.py --base-url http://localhost:8013

Numbers are only comparable within one machine and one run. Report the box you used.
"""
import argparse
import asyncio
import secrets
import statistics
import time

import httpx

PASSWORD = "BenchPassword123!"


def percentile(samples: list[float], pct: float) -> float:
    ordered = sorted(samples)
    idx = min(int(len(ordered) * pct / 100), len(ordered) - 1)
    return ordered[idx]


async def provision(base_url: str) -> str:
    """Creates a throwaway user + key, returns a short code pointing at a real URL."""
    username = f"bench_{secrets.token_hex(4)}"
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as c:
        await c.post("/auth/signup", json={"username": username, "password": PASSWORD})
        login = await c.post("/auth/login", json={"username": username, "password": PASSWORD})
        token = login.json()["access_token"]
        key = (await c.post(
            "/auth/keys", json={"label": "bench", "rate_limit": 10_000_000},
            headers={"Authorization": f"Bearer {token}"},
        )).json()["plain_key"]
        short = await c.post(
            "/shorten", json={"url": "https://example.com/benchmark-target"},
            headers={"X-API-Key": key},
        )
        return short.json()["short_code"]


async def hammer(base_url: str, path: str, duration: float, concurrency: int):
    """
    Drives `path` with a fixed pool of workers until the deadline.

    Records both client-observed latency and the server's own X-Process-Time-Ms. They
    diverge under load because the load generator is a single Python process — trust
    the server figure for per-request cost and the client figure for end-to-end wait.
    """
    client_ms: list[float] = []
    server_ms: list[float] = []
    statuses: dict[int, int] = {}
    deadline = time.perf_counter() + duration
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)

    async def worker():
        async with httpx.AsyncClient(base_url=base_url, timeout=30, limits=limits,
                                     follow_redirects=False) as c:
            while time.perf_counter() < deadline:
                started = time.perf_counter()
                try:
                    res = await c.get(path)
                    code = res.status_code
                    server_ms.append(float(res.headers.get("X-Process-Time-Ms", 0)))
                except Exception:
                    code = 0
                client_ms.append((time.perf_counter() - started) * 1000)
                statuses[code] = statuses.get(code, 0) + 1

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return client_ms, server_ms, statuses


async def cold_path_latency(base_url: str, code: str, samples: int) -> list[float]:
    """Evicts the cache before each request, so every sample pays the SQL lookup."""
    from app.cache import r  # local import: the benchmark also runs against a remote host

    out = []
    async with httpx.AsyncClient(base_url=base_url, timeout=30, follow_redirects=False) as c:
        for _ in range(samples):
            r.delete(code)
            started = time.perf_counter()
            await c.get(f"/{code}")
            out.append((time.perf_counter() - started) * 1000)
    return out


def line(label: str, samples: list[float]) -> str:
    return (f"  {label:<10} p50 {percentile(samples, 50):7.2f}  "
            f"p95 {percentile(samples, 95):7.2f}  p99 {percentile(samples, 99):7.2f} ms")


def report(name, client_ms, server_ms, elapsed, statuses=None):
    print(f"\n{name}")
    print(f"  requests   {len(client_ms):,}")
    if elapsed:
        print(f"  throughput {len(client_ms) / elapsed:,.0f} req/s")
    print(line("server", server_ms) if server_ms else "")
    print(line("client", client_ms))
    if statuses:
        print(f"  statuses   {statuses}")
        if statuses.get(429):
            print("  ! 429s present — restart the server with IP_RATE_LIMIT raised")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--concurrency", type=int, default=50)
    ap.add_argument("--cold-samples", type=int, default=100)
    args = ap.parse_args()

    code = await provision(args.base_url)
    print(f"benchmarking /{code} against {args.base_url}")

    await hammer(args.base_url, f"/{code}", 1.0, args.concurrency)  # warm-up, discarded

    for concurrency in (1, args.concurrency):
        started = time.perf_counter()
        client_ms, server_ms, statuses = await hammer(
            args.base_url, f"/{code}", args.duration, concurrency
        )
        report(f"REDIRECT — cache hit (c={concurrency})",
               client_ms, server_ms, time.perf_counter() - started, statuses)

    try:
        cold = await cold_path_latency(args.base_url, code, args.cold_samples)
        report("REDIRECT — cache miss, serial (DB fallback)", cold, [], 0)
    except Exception as e:
        print(f"\nskipped cold-path measurement (needs local Redis access): {e}")


if __name__ == "__main__":
    asyncio.run(main())
