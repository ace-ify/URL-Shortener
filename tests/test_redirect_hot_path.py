"""Redirect hot path: cache hit, cache miss, expiry, soft delete, and the click queue."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


def _make_link(client, dev, url="https://example.com/hot", **extra):
    res = client.post("/shorten", json={"url": url, **extra}, headers=dev.api)
    assert res.status_code == 201
    return res.json()["short_code"]


def test_cache_hit_redirects_without_touching_the_database(client, make_dev, redis_stub):
    dev = make_dev("cachehit")
    code = _make_link(client, dev)

    with patch("app.cache.r.get", return_value="https://cached.example.com/dest"), \
         patch("app.crud.get_url_by_code") as db_lookup:
        res = client.get(f"/{code}", follow_redirects=False)

    assert res.status_code == 307
    assert res.headers["location"] == "https://cached.example.com/dest"
    db_lookup.assert_not_called()  # hot path must not query SQL on a cache hit


def test_cache_miss_falls_back_to_db_and_warms_the_cache(client, make_dev, redis_stub):
    dev = make_dev("cachemiss")
    target = "https://example.com/miss"
    code = _make_link(client, dev, url=target)
    redis_stub.cache_set.reset_mock()

    res = client.get(f"/{code}", follow_redirects=False)

    assert res.status_code == 307
    assert res.headers["location"] == target
    cached = {c.args[0]: c.args[1] for c in redis_stub.cache_set.call_args_list}
    assert cached.get(code) == target


def test_every_redirect_enqueues_exactly_one_click_event(client, make_dev, redis_stub):
    dev = make_dev("clicks")
    code = _make_link(client, dev)
    redis_stub.rpush.reset_mock()

    for _ in range(3):
        client.get(f"/{code}", follow_redirects=False)

    assert redis_stub.rpush.call_count == 3
    queue, raw = redis_stub.rpush.call_args[0]
    assert queue == "click_events_queue"
    event = json.loads(raw)
    assert event["short_code"] == code
    assert isinstance(event["timestamp"], float)


def test_redirect_survives_a_dead_queue(client, make_dev):
    """Analytics is best-effort: a Redis outage must not break the redirect."""
    dev = make_dev("queuedown")
    code = _make_link(client, dev)

    with patch("app.queue.r.rpush", side_effect=Exception("connection refused")):
        res = client.get(f"/{code}", follow_redirects=False)

    assert res.status_code == 307


def test_unknown_expired_and_deleted_codes_map_to_distinct_statuses(client, make_dev):
    dev = make_dev("statuses")
    live = _make_link(client, dev)
    expired = _make_link(
        client, dev, url="https://example.com/old", expires_at="2020-01-01T00:00:00Z"
    )

    assert client.get("/no-such-code", follow_redirects=False).status_code == 404
    assert client.get(f"/{expired}", follow_redirects=False).status_code == 410

    client.delete(f"/urls/{live}", headers=dev.jwt)
    assert client.get(f"/{live}", follow_redirects=False).status_code == 404


def test_cache_ttl_is_clamped_to_the_link_expiry(client, make_dev, redis_stub):
    """
    Regression: the hot path answers from cache without reading SQL, so a flat 2h TTL
    kept serving 307s for links that had already expired. TTL must end at the deadline.
    """
    dev = make_dev("ttlclamp")
    soon = datetime.now(timezone.utc) + timedelta(minutes=5)

    client.post(
        "/shorten",
        json={"url": "https://example.com/soon", "expires_at": soon.isoformat()},
        headers=dev.api,
    )

    ttl = redis_stub.cache_set.call_args.kwargs["ex"]
    assert 0 < ttl <= 300, f"TTL {ttl} outlives the link"


def test_already_expired_links_are_never_written_to_the_cache(client, make_dev, redis_stub):
    dev = make_dev("noexpcache")
    redis_stub.cache_set.reset_mock()

    with patch("app.cache.r.delete") as evict:
        code = _make_link(
            client, dev, url="https://example.com/dead", expires_at="2020-01-01T00:00:00Z"
        )

    redis_stub.cache_set.assert_not_called()
    evict.assert_called_once_with(code)


def test_soft_delete_evicts_the_link_from_the_hot_path(client, make_dev):
    """Regression: deletes only touched SQL, so cached links redirected for another 2h."""
    dev = make_dev("evictdel")
    code = _make_link(client, dev)

    with patch("app.cache.r.delete") as evict:
        assert client.delete(f"/urls/{code}", headers=dev.jwt).status_code == 200

    evict.assert_called_once_with(code)


def test_redirects_survive_a_total_redis_outage(client, make_dev):
    """
    Regression: Redis is an accelerator, not the source of truth. A cache lookup that
    raised (or a rate limiter that raised) turned every redirect into a 500, so the
    outage of an optional dependency took down the one path that must stay up.
    """
    dev = make_dev("redisdown")
    target = "https://example.com/resilient"
    code = _make_link(client, dev, url=target)
    boom = Exception("Error 111 connecting to localhost:6379. Connection refused.")

    with patch("app.cache.r.get", side_effect=boom), \
         patch("app.cache.r.set", side_effect=boom), \
         patch("app.rate_limiter.r.zremrangebyscore", side_effect=boom), \
         patch("app.rate_limiter.r.zcard", side_effect=boom), \
         patch("app.rate_limiter.r.zadd", side_effect=boom), \
         patch("app.queue.r.rpush", side_effect=boom):
        res = client.get(f"/{code}", follow_redirects=False)

    assert res.status_code == 307
    assert res.headers["location"] == target


def test_redis_client_is_configured_to_fail_fast(client):
    """
    The fallbacks above are only graceful if the client gives up quickly. redis-py
    retries connection errors with exponential backoff by default, which turned an
    unreachable cache into a multi-second stall per call — measured at ~8.7s, and
    an indefinite hang across a full redirect. Guard the config, not just the fallback.
    """
    from app.cache import r

    kwargs = r.connection_pool.connection_kwargs
    assert 0 < kwargs["socket_connect_timeout"] <= 1
    assert 0 < kwargs["socket_timeout"] <= 1
    assert kwargs["retry"].get_retries() == 0


def test_rate_limiter_fails_open_rather_than_closed(client, make_dev):
    """An unreachable limiter must not manufacture 429s for legitimate traffic."""
    dev = make_dev("failopen")

    with patch("app.rate_limiter.r.zcard", side_effect=Exception("redis down")):
        res = client.post("/shorten", json={"url": "https://example.com/fo"}, headers=dev.api)

    assert res.status_code == 201


def test_future_expiry_still_redirects(client, make_dev):
    dev = make_dev("futureexp")
    code = _make_link(
        client, dev, url="https://example.com/later", expires_at="2099-01-01T00:00:00Z"
    )
    assert client.get(f"/{code}", follow_redirects=False).status_code == 307
