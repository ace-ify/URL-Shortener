import json
import time
from app.cache import r
from app.database import SessionLocal
from app import crud

QUEUE_NAME = "click_events_queue"
DLQ_NAME = "click_events_dlq"
MAX_RETRIES = 3

def process_single_event(short_code: str) -> bool:
    """Attempts DB click increment with Session management."""
    db = SessionLocal()
    try:
        crud.increment_clicks(db, short_code)
        return True
    except Exception as e:
        print(f"❌ [Worker DB Error] Failed to update clicks for {short_code}: {e}")
        return False
    finally:
        db.close()

def handle_click_event(event: dict) -> str:
    """
    Processes one dequeued event. Returns the outcome so the retry/DLQ policy is
    testable without running the blocking loop: "ok" | "retried" | "dlq" | "skipped".
    """
    short_code = event.get("short_code")
    if not short_code:
        return "skipped"

    if process_single_event(short_code):
        print(f"✅ [Analytics Worker] Successfully persisted click for short_code: '{short_code}'")
        return "ok"

    retry_count = event.get("retry_count", 0) + 1
    if retry_count <= MAX_RETRIES:
        event["retry_count"] = retry_count
        print(f"⚠️ [Analytics Worker Retry] Retrying short_code '{short_code}' ({retry_count}/{MAX_RETRIES})...")
        r.rpush(QUEUE_NAME, json.dumps(event))
        time.sleep(0.5 * (2 ** retry_count))  # Exponential Backoff
        return "retried"

    print(f"🚨 [Analytics Worker DLQ] Max retries reached for '{short_code}'. Pushing to Dead-Letter Queue '{DLQ_NAME}'!")
    r.rpush(DLQ_NAME, json.dumps(event))
    return "dlq"


def run_worker():
    print(f"🚀 [Analytics Worker] Started! Listening on Redis queue '{QUEUE_NAME}' (DLQ: '{DLQ_NAME}')...")
    while True:
        try:
            # BLPOP: Redis blocking pop (waits up to 5 seconds for a new event)
            result = r.blpop(QUEUE_NAME, timeout=5)
            if not result:
                continue

            _, payload_str = result
            handle_click_event(json.loads(payload_str))

        except Exception as e:
            print(f"⚠️ [Analytics Worker Loop Error] {e}")
            time.sleep(1)

if __name__ == "__main__":
    run_worker()