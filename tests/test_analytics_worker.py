"""Async analytics consumer: DB persistence, bounded retries, dead-letter queue."""
import json
from unittest.mock import patch

import pytest

from app import worker
from app.models import URLModel


@pytest.fixture
def worker_db(db_session):
    """Point the worker's own session factory at the in-memory test DB."""
    with patch("app.worker.SessionLocal", lambda: db_session), \
         patch("app.worker.time.sleep"):
        yield db_session


def test_worker_persists_a_buffered_click(client, make_dev, worker_db):
    dev = make_dev("persist")
    code = client.post(
        "/shorten", json={"url": "https://example.com/w"}, headers=dev.api
    ).json()["short_code"]

    assert worker.handle_click_event({"short_code": code}) == "ok"
    assert worker.handle_click_event({"short_code": code}) == "ok"

    assert worker_db.query(URLModel).filter_by(short_code=code).one().clicks == 2


def test_failed_write_is_requeued_with_an_incremented_retry_count(worker_db):
    with patch("app.worker.process_single_event", return_value=False), \
         patch("app.worker.r.rpush") as rpush:
        outcome = worker.handle_click_event({"short_code": "abc123", "retry_count": 1})

    assert outcome == "retried"
    queue, raw = rpush.call_args[0]
    assert queue == worker.QUEUE_NAME
    assert json.loads(raw)["retry_count"] == 2


def test_poison_event_lands_in_the_dlq_after_max_retries(worker_db):
    with patch("app.worker.process_single_event", return_value=False), \
         patch("app.worker.r.rpush") as rpush:
        outcome = worker.handle_click_event(
            {"short_code": "poison", "retry_count": worker.MAX_RETRIES}
        )

    assert outcome == "dlq"
    queue, raw = rpush.call_args[0]
    assert queue == worker.DLQ_NAME
    assert json.loads(raw)["short_code"] == "poison"


def test_retry_backoff_grows_exponentially(worker_db):
    delays = []
    with patch("app.worker.process_single_event", return_value=False), \
         patch("app.worker.r.rpush"), \
         patch("app.worker.time.sleep", side_effect=delays.append):
        for attempt in range(worker.MAX_RETRIES):
            worker.handle_click_event({"short_code": "slow", "retry_count": attempt})

    assert delays == sorted(delays) and delays[0] < delays[-1]


def test_malformed_event_is_skipped_not_retried(worker_db):
    with patch("app.worker.r.rpush") as rpush:
        assert worker.handle_click_event({"timestamp": 1.0}) == "skipped"
    rpush.assert_not_called()


def test_db_failure_is_swallowed_and_reported_as_failure(worker_db):
    with patch("app.worker.crud.increment_clicks", side_effect=Exception("db down")):
        assert worker.process_single_event("anycode") is False
