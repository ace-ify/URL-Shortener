import time
from typing import Optional

from fastapi import Request, HTTPException, status, Depends
from app.cache import r
from app.config import settings
from app.auth import get_api_key_owner
from app.models import UserModel

# --- TIER 1: PER-IP RATE LIMITER (For Public Endpoints) ---
IP_RATE_LIMIT = settings.ip_rate_limit  # Max requests per minute per IP
WINDOW_SIZE = 60                        # 60 seconds sliding window


def _within_quota(key: str, limit: int) -> bool:
    """
    Sliding window in a single round-trip: prune, record, count and refresh the TTL
    inside one MULTI. Measured at 4 round-trips before batching, which dominated
    redirect latency — Redis is fast, but the network hop is not free.

    The hit is recorded before it is counted, so a rejected request still occupies a
    slot and a flood does not earn free retries.

    Fails open when Redis is unreachable: during a cache outage we choose availability
    over enforcement, since the alternative is an outage of the public redirect path.
    The warning log is the alerting signal.
    """
    now = time.time()
    try:
        with r.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(key, 0, now - WINDOW_SIZE)
            pipe.zadd(key, {str(now): now})
            pipe.zcard(key)
            pipe.expire(key, WINDOW_SIZE)
            hits = pipe.execute()[2]
    except Exception as e:
        print(f"[Rate Limiter Warning] Redis unavailable, failing open for {key}: {e}")
        return True

    return hits <= limit


def limit_ip_rate(request: Request):
    """Tier 1: protects public redirects from a single noisy source IP."""
    client_ip = request.client.host if request.client else "127.0.0.1"

    if not _within_quota(f"rate_limit:ip:{client_ip}", IP_RATE_LIMIT):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests from this IP. Please wait a minute."
        )


# --- TIER 2: PER-API-KEY RATE LIMITER (For Developer Programmatic API) ---
def limit_api_key_rate(user: UserModel = Depends(get_api_key_owner)) -> UserModel:
    """Authenticates the API Key AND enforces the per-key quota stored on its DB row."""
    key_record = getattr(user, "active_api_key", None) or getattr(user, "active_api_key_record", None)
    if not key_record:
        return user

    rate_limit = key_record.rate_limit  # Dynamic limit set per API Key in DB

    if not _within_quota(f"rate_limit:apikey:{key_record.key_hash}", rate_limit):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"API Key rate limit quota exceeded ({rate_limit} requests/min limit)."
        )
    return user
