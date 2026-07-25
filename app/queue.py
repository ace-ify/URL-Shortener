import json
import time
from app.cache import r

QUEUE_NAME = "click_events_queue"

def push_click_event(short_code: str):
    """Pushes a click event to Redis queue list in < 0.2ms (Non-blocking hot path)"""
    event = {
        "short_code":short_code,
        "timestamp":time.time()
    }
    try:
        r.rpush(QUEUE_NAME,json.dumps(event))
    except Exception as e:
        print(f"[Queue Warning] Failed to push click event for {short_code}: {e}")