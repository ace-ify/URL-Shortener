import time
from fastapi import Request, HTTPException, status, Depends
from app.cache import r
from app.auth import get_api_key_owner
from app.models import UserModel

# --- TIER 1: PER-IP RATE LIMITER (For Public Endpoints) ---
IP_RATE_LIMIT = 30  # Max 30 requests per minute per IP
WINDOW_SIZE = 60    # 60 seconds sliding window

def limit_ip_rate(request: Request):
    client_ip = request.client.host if request.client else "127.0.0.1"
    key = f"rate_limit:ip:{client_ip}"
    current_time = time.time()

    # Redis Sliding Window: Purane records clear karo (older than 60s)
    r.zremrangebyscore(key, 0, current_time - WINDOW_SIZE)
    request_count = r.zcard(key)

    if request_count >= IP_RATE_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests from this IP. Please wait a minute."
        )

    r.zadd(key, {str(current_time): current_time})
    r.expire(key, WINDOW_SIZE)

# --- TIER 2: PER-API-KEY RATE LIMITER (For Developer Programmatic API) ---
def limit_api_key_rate(user: UserModel = Depends(get_api_key_owner)) -> UserModel:
    """Dependency that authenticates the API Key AND enforces its custom rate limit quota"""
    key_record = getattr(user, "active_api_key", None) or getattr(user, "active_api_key_record", None)
    if not key_record:
        return user
        
    key_hash = key_record.key_hash
    rate_limit = key_record.rate_limit  # Dynamic limit set per API Key in DB
    key = f"rate_limit:apikey:{key_hash}"
    current_time = time.time()

    # Redis Sliding Window
    r.zremrangebyscore(key, 0, current_time - WINDOW_SIZE)
    request_count = r.zcard(key)

    if request_count >= rate_limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"API Key rate limit quota exceeded ({rate_limit} requests/min limit)."
        )

    r.zadd(key, {str(current_time): current_time})
    r.expire(key, WINDOW_SIZE)
    return user
