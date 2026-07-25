import json
import time
from app.cache import r
from app.database import SessionLocal
from app import crud

QUEUE_NAME = "click_events_queue"

def run_worker():
    print("🚀 [Analytics Worker] Started! Listening for click events on Redis queue...")
    while True:
        try:
            # BLPOP: Redis blocking pop (waits up to 5 seconds for a new event)
            result = r.blpop(QUEUE_NAME, timeout=5)
            if not result:
                continue
                
            # result is a tuple: (queue_name, payload_json_string)
            _, payload_str = result
            event = json.loads(payload_str)
            short_code = event.get("short_code")
            
            if short_code:
                db = SessionLocal()
                try:
                    crud.increment_clicks(db, short_code)
                    print(f"✅ [Analytics Worker] Incremented click count for short_code: {short_code}")
                except Exception as db_err:
                    print(f"❌ [Analytics Worker DB Error] {db_err}")
                finally:
                    db.close()
        except Exception as e:
            print(f"⚠️ [Analytics Worker Error] {e}")
            time.sleep(1)
if __name__ == "__main__":
    run_worker()