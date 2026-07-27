import pytest
from unittest.mock import patch
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app

# Test database setup (In-Memory SQLite)
SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Override get_db dependency to use Test DB
def override_get_db():
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

@pytest.fixture(autouse=True)
def setup_db_and_redis():
    Base.metadata.create_all(bind=engine)
    with patch("app.rate_limiter.r.zremrangebyscore", return_value=0), \
         patch("app.rate_limiter.r.zcard", return_value=1), \
         patch("app.rate_limiter.r.zadd", return_value=1), \
         patch("app.rate_limiter.r.expire", return_value=True), \
         patch("app.cache.r.get", return_value=None), \
         patch("app.cache.r.set", return_value=True), \
         patch("app.cache.r.ping", return_value=True), \
         patch("app.queue.r.rpush", return_value=1):
        yield
    Base.metadata.drop_all(bind=engine)

client = TestClient(app)

# 1. AUTHENTICATION TESTS
def test_user_signup_and_login():
    # Weak password test (OWASP check)
    weak_res = client.post("/auth/signup", json={"username": "user1", "password": "123"})
    assert weak_res.status_code == 422  # Validation error

    long_res = client.post("/auth/signup", json={"username": "longuser", "password": "A1!" + "a" * 70})
    assert long_res.status_code == 422

    # Valid signup
    signup_res = client.post("/auth/signup", json={"username": "user1", "password": "Password123!"})
    assert signup_res.status_code == 201

    # Login and get JWT token
    login_res = client.post("/auth/login", json={"username": "user1", "password": "Password123!"})
    assert login_res.status_code == 200
    assert "access_token" in login_res.json()

# 2. DEVELOPER API KEY TESTS
def test_api_key_lifecycle():
    client.post("/auth/signup", json={"username": "dev1", "password": "Password123!"})
    login_res = client.post("/auth/login", json={"username": "dev1", "password": "Password123!"})
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Create API Key
    key_res = client.post("/auth/keys", json={"label": "Test Key", "rate_limit": 5}, headers=headers)
    assert key_res.status_code == 201
    plain_key = key_res.json()["plain_key"]
    assert plain_key.startswith("sk_live_")

    # Shorten URL using API Key
    shorten_res = client.post(
        "/shorten", 
        json={"url": "https://example.com/test"}, 
        headers={"X-API-Key": plain_key}
    )
    assert shorten_res.status_code == 201
    assert "short_code" in shorten_res.json()

def test_ssrf_url_is_rejected():
    client.post("/auth/signup", json={"username": "ssrfuser", "password": "Password123!"})
    token = client.post("/auth/login", json={"username": "ssrfuser", "password": "Password123!"}).json()["access_token"]
    key = client.post("/auth/keys", json={}, headers={"Authorization": f"Bearer {token}"}).json()["plain_key"]

    response = client.post("/shorten", json={"url": "http://127.0.0.1/admin"}, headers={"X-API-Key": key})
    assert response.status_code == 422
    assert "forbidden" in response.json()["error"]["details"][0]["msg"].lower()

def test_short_code_generation_retries_after_collision():
    with patch("app.main.generate_base62_code", side_effect=["taken1", "fresh2"]), \
         patch("app.main.crud.get_url_by_code", side_effect=[object(), None]) as get_by_code:
        from app.main import generate_collision_safe_short_code
        assert generate_collision_safe_short_code(object()) == "fresh2"
    assert get_by_code.call_count == 2

# 3. OWNERSHIP ENFORCEMENT & SOFT DELETE TESTS
def test_ownership_and_soft_delete():
    # Setup User A and User B
    client.post("/auth/signup", json={"username": "usera", "password": "Password123!"})
    token_a = client.post("/auth/login", json={"username": "usera", "password": "Password123!"}).json()["access_token"]
    key_a = client.post("/auth/keys", json={}, headers={"Authorization": f"Bearer {token_a}"}).json()["plain_key"]

    client.post("/auth/signup", json={"username": "userb", "password": "Password123!"})
    token_b = client.post("/auth/login", json={"username": "userb", "password": "Password123!"}).json()["access_token"]

    # User A creates a URL
    short_res = client.post("/shorten", json={"url": "https://google.com"}, headers={"X-API-Key": key_a})
    short_code = short_res.json()["short_code"]

    # User B cannot change or delete User A's URL.
    update_res = client.patch(
        f"/urls/{short_code}",
        json={"new_original_url": "https://example.com/changed"},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert update_res.status_code == 403

    del_res = client.delete(f"/urls/{short_code}", headers={"Authorization": f"Bearer {token_b}"})
    assert del_res.status_code == 403

    # User A deletes their own URL -> Soft Delete Success (200)
    del_own = client.delete(f"/urls/{short_code}", headers={"Authorization": f"Bearer {token_a}"})
    assert del_own.status_code == 200

    # Public redirect to soft-deleted URL should return 404
    redir = client.get(f"/{short_code}")
    assert redir.status_code == 404

def test_health_liveness_and_readiness():
    # 1. Test Liveness Probe (Always 200 OK immediately)
    live_res = client.get("/health/live")
    assert live_res.status_code == 200
    assert live_res.json() == {"status": "alive"}

    # 2. Test Readiness Probe (Healthy State)
    with patch("app.cache.r.ping", return_value=True):
        ready_res = client.get("/health/ready")
        assert ready_res.status_code == 200
        assert ready_res.json()["status"] == "ready"
        assert ready_res.json()["checks"]["database"] == "healthy"
        assert ready_res.json()["checks"]["redis"] == "healthy"

    # 3. Test Readiness Probe when Redis is DOWN -> Must return 503 Service Unavailable
    with patch("app.cache.r.ping", side_effect=Exception("Redis connection refused")):
        unready_res = client.get("/health/ready")
        assert unready_res.status_code == 503
        error_detail = unready_res.json()["error"]["message"]
        assert error_detail["status"] == "unready"
        assert "unhealthy: Redis connection refused" in error_detail["checks"]["redis"]

def test_logging_middleware_redacts_pii_and_secrets(caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="api.access"):
        # Send signup request containing sensitive password field
        res = client.post(
            "/auth/signup", 
            json={"username": "secretuser", "password": "Password123!"},
            headers={"Authorization": "Bearer supersecretjwt", "X-API-Key": "sk_live_supersecret"}
        )
        assert res.status_code == 201
        assert "X-Process-Time-Ms" in res.headers

        # Assert log line was captured and sensitive values were redacted
        log_text = caplog.text
        assert "secretuser" in log_text
        assert "Password123!" not in log_text  # MUST BE REDACTED
        assert "supersecretjwt" not in log_text  # MUST BE REDACTED
        assert "sk_live_supersecret" not in log_text  # MUST BE REDACTED
        assert "[REDACTED]" in log_text

def test_api_versioning_v1_and_v2():
    client.post("/auth/signup", json={"username": "vuser", "password": "Password123!"})
    token = client.post("/auth/login", json={"username": "vuser", "password": "Password123!"}).json()["access_token"]
    key = client.post("/auth/keys", json={}, headers={"Authorization": f"Bearer {token}"}).json()["plain_key"]

    # 1. Test /v1/shorten -> Expect Deprecation / Sunset headers
    v1_res = client.post("/v1/shorten", json={"url": "https://example.com/v1test"}, headers={"X-API-Key": key})
    assert v1_res.status_code == 201
    assert "Sunset" in v1_res.headers
    assert v1_res.headers.get("X-API-Deprecated") == "true"
    assert "short_code" in v1_res.json()

    # 2. Test /v2/shorten -> Expect V2 breaking schema (nested under 'data' and 'api_version')
    v2_res = client.post("/v2/shorten", json={"url": "https://example.com/v2test"}, headers={"X-API-Key": key})
    assert v2_res.status_code == 201
    assert "Sunset" not in v2_res.headers
    body_v2 = v2_res.json()
    assert body_v2["api_version"] == "v2"
    assert "target_url" in body_v2["data"]
    assert body_v2["data"]["target_url"] == "https://example.com/v2test"

def test_google_oauth_flow_and_account_linking():
    # 1. Initiate OAuth Login -> Generates state parameter
    login_res = client.get("/auth/google/login")
    assert login_res.status_code == 200
    state = login_res.json()["state"]
    assert "authorization_url" in login_res.json()
    assert state in login_res.json()["authorization_url"]

    # 2. Callback with INVALID state parameter -> Expect 400 CSRF error
    invalid_cb = client.get(f"/auth/google/callback?code=mock_code&state=invalid_csrf_state")
    assert invalid_cb.status_code == 400
    assert "CSRF" in invalid_cb.json()["error"]["message"]

    # 3. Callback with VALID state parameter -> Successfully issues platform JWT
    with patch("app.main.fetch_google_user_profile", return_value={"email": "google_user@example.com", "sub": "sub_9999"}):
        valid_cb = client.get(f"/auth/google/callback?code=mock_code&state={state}")
        assert valid_cb.status_code == 200
        assert "access_token" in valid_cb.json()
        assert valid_cb.json()["email"] == "google_user@example.com"




