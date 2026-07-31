import redis
from app.config import settings

r = redis.Redis(
    host=settings.redis_host,
    port=settings.redis_port,
    db=settings.redis_db,
    decode_responses=True
)

def get_cached_url(short_code: str) -> str:
    return r.get(short_code)

def set_cached_url(short_code: str, original_url: str):
    """Caches original URL mapping in Redis with a 2-hour TTL"""
    try:
        r.set(short_code, original_url, ex=7200)  # Modern syntax replacing deprecated setex
    except Exception as e:
        print(f"[Redis Cache Warning] Could not set cache for {short_code}: {e}")

def increment_click_buffer(short_code: str) -> int:
    """Atomically increments the click counter in Redis memory in 0.1ms (High-throughput buffer)."""
    try:
        return r.incr(f"clicks_buffer:{short_code}")
    except Exception as e:
        print(f"[Redis Click Buffer Warning] Could not increment buffer for {short_code}: {e}")
        return 1

def get_buffered_clicks(short_code: str) -> int:
    """Retrieves accumulated click counts from Redis buffer."""
    try:
        val = r.get(f"clicks_buffer:{short_code}")
        return int(val) if val else 0
    except Exception:
        return 0

