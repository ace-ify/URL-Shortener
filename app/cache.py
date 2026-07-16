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
    r.setex(short_code, 7200, original_url)