import time
from fastapi import Request, HTTPException, status
from app.cache import r

RATE_LIMIT=5
WINDOW_SIZE=60

def limit_rate(request: Request):
    client_ip = request.client.host
    key = f"rate_limit:{client_ip}"
    current_time=time.time()

    r.zremrangebyscore(key,0,current_time-WINDOW_SIZE)

    request_count = r.zcard(key)

    if request_count>=RATE_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests"
        )
    r.zadd(key,{str(current_time):current_time})
    r.expire(key,WINDOW_SIZE)