"""Shared test rig: in-memory SQLite + stubbed Redis, reused by every test module."""
import hashlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


class FakePipeline:
    """
    Stands in for a Redis MULTI. Queues commands like the real client and returns
    canned results positionally: [prune, zadd, hit_count, expire].
    `hits` is the window count *including* the request being checked.
    """

    hits = 1   # class-level so a test can dial the window up without rebuilding the stub
    last = None  # most recently executed pipeline, for asserting the command batch

    def __init__(self):
        self.commands = []
        FakePipeline.last = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _queue(self, name):
        self.commands.append(name)
        return self

    def zremrangebyscore(self, *a): return self._queue("zremrangebyscore")
    def zadd(self, *a, **kw): return self._queue("zadd")
    def zcard(self, *a): return self._queue("zcard")
    def expire(self, *a): return self._queue("expire")

    def execute(self):
        return [0, 1, FakePipeline.hits, True]


class StateStore:
    """Stands in for the Redis keys backing single-use OAuth state (SET / DELETE only)."""

    def __init__(self):
        self.keys = set()

    def set(self, key, value, ex=None):
        self.keys.add(key)
        return True

    def delete(self, key):
        existed = key in self.keys
        self.keys.discard(key)
        return 1 if existed else 0


@pytest.fixture(autouse=True)
def redis_stub():
    """
    No real Redis in CI. Defaults: rate-limit window reports 1 hit (always under
    quota), cache always misses (forces the DB path), queue push succeeds.
    Tests override a single mock to exercise the other branch.
    """
    Base.metadata.create_all(bind=engine)
    states = StateStore()
    FakePipeline.hits = 1  # every test starts with an empty rate-limit window
    with patch("app.auth.r", states), \
         patch("app.rate_limiter.r.pipeline", side_effect=lambda **kw: FakePipeline()) as pipeline, \
         patch("app.cache.r.get", return_value=None) as cache_get, \
         patch("app.cache.r.set", return_value=True) as cache_set, \
         patch("app.cache.r.ping", return_value=True), \
         patch("app.queue.r.rpush", return_value=1) as rpush:
        yield SimpleNamespace(
            pipeline=pipeline, window=FakePipeline, cache_get=cache_get,
            cache_set=cache_set, rpush=rpush, states=states,
        )
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db_session():
    """Direct DB handle for arranging state the API cannot set (e.g. click counts)."""
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def make_dev(client):
    """Factory: signed-up user carrying a JWT and a developer API key."""
    def _make(username, password="Password123!", rate_limit=10):
        client.post("/auth/signup", json={"username": username, "password": password})
        token = client.post(
            "/auth/login", json={"username": username, "password": password}
        ).json()["access_token"]
        jwt_headers = {"Authorization": f"Bearer {token}"}
        key = client.post(
            "/auth/keys", json={"label": username, "rate_limit": rate_limit}, headers=jwt_headers
        ).json()["plain_key"]
        return SimpleNamespace(
            username=username,
            token=token,
            key=key,
            jwt=jwt_headers,
            api={"X-API-Key": key},
            key_sha=hashlib.sha256(key.encode()).hexdigest(),
        )
    return _make
