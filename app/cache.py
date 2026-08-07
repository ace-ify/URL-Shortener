from datetime import datetime, timezone
from typing import Optional

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.config import settings

# Bounded timeouts are what make the fallbacks below actually graceful. Without them a
# dead Redis does not raise — it blocks the worker thread on connect, so "fall back to
# the DB" never runs and an optional dependency still takes the redirect path down.
# Measured: an unreachable Redis hung requests indefinitely before these were set.
r = redis.Redis(
    host=settings.redis_host,
    port=settings.redis_port,
    db=settings.redis_db,
    decode_responses=True,
    socket_connect_timeout=settings.redis_timeout_seconds,
    socket_timeout=settings.redis_timeout_seconds,
    # redis-py 6+ retries connection errors with exponential backoff by default, which
    # turned a 0.25s timeout into an ~8.7s stall per call. The hot path wants to know
    # immediately that the cache is gone, not to keep trying on the user's clock.
    retry=Retry(NoBackoff(), 0),
)

MAX_TTL_SECONDS = 7200  # 2 hours

def get_cached_url(short_code: str) -> Optional[str]:
    """
    Reads the destination from the hot path.

    Returns None on a Redis outage instead of raising, so the redirect degrades to a
    slower database lookup rather than 500ing. Redis is an accelerator here, not the
    source of truth, and must not be a single point of failure for redirects.
    """
    try:
        return r.get(short_code)
    except Exception as e:
        print(f"[Redis Cache Warning] Lookup failed for {short_code}, falling back to DB: {e}")
        return None

def set_cached_url(short_code: str, original_url: str, expires_at: Optional[datetime] = None):
    """
    Caches the destination for 2 hours, or until the link expires — whichever comes first.

    The redirect hot path answers from this cache without reading SQL, so the TTL is the
    only thing that can enforce expiry there. Letting Redis evict the key at the deadline
    drops the request onto the DB path, which returns the correct HTTP 410.
    """
    ttl = MAX_TTL_SECONDS
    if expires_at:
        exp_utc = expires_at.replace(tzinfo=timezone.utc) if expires_at.tzinfo is None else expires_at
        ttl = min(ttl, int((exp_utc - datetime.now(timezone.utc)).total_seconds()))
        if ttl <= 0:
            delete_cached_url(short_code)  # already dead: must never be served from cache
            return
    try:
        r.set(short_code, original_url, ex=ttl)  # Modern syntax replacing deprecated setex
    except Exception as e:
        print(f"[Redis Cache Warning] Could not set cache for {short_code}: {e}")

def delete_cached_url(short_code: str):
    """Evicts a link from the hot path (soft delete), so it stops redirecting immediately."""
    try:
        r.delete(short_code)
    except Exception as e:
        print(f"[Redis Cache Warning] Could not evict cache for {short_code}: {e}")

# The clicks_buffer INCR helpers that used to live here were superseded by the
# queue + worker pipeline (see ADR-007) and removed rather than left as dead code.

