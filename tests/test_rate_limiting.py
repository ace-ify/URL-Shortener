"""Two-tier sliding-window rate limiting (the Redis MULTI is stubbed by conftest)."""
from unittest.mock import patch

from app.rate_limiter import IP_RATE_LIMIT, WINDOW_SIZE


def test_public_redirect_allowed_while_ip_window_has_room(client, make_dev, redis_stub):
    dev = make_dev("ip_ok")
    code = client.post(
        "/shorten", json={"url": "https://example.com/a"}, headers=dev.api
    ).json()["short_code"]

    redis_stub.window.hits = IP_RATE_LIMIT
    assert client.get(f"/{code}", follow_redirects=False).status_code == 307


def test_public_redirect_429_when_ip_window_is_full(client, redis_stub):
    redis_stub.window.hits = IP_RATE_LIMIT + 1

    res = client.get("/anything", follow_redirects=False)

    assert res.status_code == 429
    assert "Too many requests" in res.json()["error"]["message"]


def test_api_key_quota_is_read_per_key_not_global(client, make_dev, redis_stub):
    """Quota comes from the key's own DB row, so the same window trips one key not the other."""
    tight = make_dev("tight_key", rate_limit=1)
    roomy = make_dev("roomy_key", rate_limit=100)
    payload = {"url": "https://example.com/quota"}

    redis_stub.window.hits = 2  # two hits in the window, including this request
    blocked = client.post("/shorten", json=payload, headers=tight.api)
    allowed = client.post("/shorten", json=payload, headers=roomy.api)

    assert blocked.status_code == 429
    assert "1 requests/min" in blocked.json()["error"]["message"]
    assert allowed.status_code == 201


def test_window_is_pruned_recorded_and_expired_in_one_round_trip(client, make_dev, redis_stub):
    """
    Batching matters: four sequential round-trips per request dominated redirect
    latency. Prune/record/count/expire must ship as one MULTI, in that order.
    """
    dev = make_dev("batched")
    redis_stub.pipeline.reset_mock()

    client.post("/shorten", json={"url": "https://example.com/prune"}, headers=dev.api)

    assert redis_stub.pipeline.call_count == 1
    assert redis_stub.pipeline.call_args.kwargs == {"transaction": True}
    assert redis_stub.window.last.commands == ["zremrangebyscore", "zadd", "zcard", "expire"]


def test_rejected_requests_still_consume_a_slot(client, redis_stub):
    """A blocked flood must not get free retries — the hit is recorded before counting."""
    redis_stub.window.hits = IP_RATE_LIMIT + 1

    assert client.get("/anything", follow_redirects=False).status_code == 429
    assert redis_stub.window.last.commands.count("zadd") == 1
