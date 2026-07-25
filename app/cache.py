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
