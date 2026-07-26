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
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)

client = TestClient(app)

# 1. AUTHENTICATION TESTS
def test_user_signup_and_login():
    # Weak password test (OWASP check)
    weak_res = client.post("/auth/signup", json={"username": "user1", "password": "123"})
    assert weak_res.status_code == 422  # Validation error

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

def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
