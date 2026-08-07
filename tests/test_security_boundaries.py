"""Auth boundaries, secret handling, and the standardized error envelope."""
import hashlib
from datetime import timedelta
from unittest.mock import patch

from app.auth import create_access_token
from app.middleware import redact_dict
from app.models import APIKeyModel


def test_api_key_is_persisted_only_as_a_hash(client, make_dev, db_session):
    dev = make_dev("hashonly")

    rows = db_session.query(APIKeyModel).all()
    stored = {r.key_hash for r in rows}

    assert dev.key not in stored, "plaintext API key must never reach the database"
    assert hashlib.sha256(dev.key.encode()).hexdigest() in stored
    # the listing endpoint exposes the prefix only, never the secret
    listed = client.get("/auth/keys", headers=dev.jwt).json()
    assert all(k["plain_key"] is None for k in listed)
    assert listed[0]["prefix"] == "sk_live"


def test_api_key_endpoints_reject_missing_and_forged_keys(client):
    payload = {"url": "https://example.com/x"}

    missing = client.post("/shorten", json=payload)
    forged = client.post("/shorten", json=payload, headers={"X-API-Key": "sk_live_not_a_real_key"})

    assert missing.status_code == 401
    assert "Missing X-API-Key" in missing.json()["error"]["message"]
    assert forged.status_code == 401
    assert "Invalid API Key" in forged.json()["error"]["message"]


def test_jwt_routes_reject_missing_garbage_and_expired_tokens(client, make_dev):
    make_dev("jwtuser")
    expired = create_access_token({"sub": "jwtuser"}, expires_delta=timedelta(minutes=-5))
    unknown_user = create_access_token({"sub": "ghost_user"})

    assert client.get("/urls").status_code == 401
    assert client.get("/urls", headers={"Authorization": "Bearer not.a.jwt"}).status_code == 401
    assert client.get("/urls", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    assert client.get("/urls", headers={"Authorization": f"Bearer {unknown_user}"}).status_code == 401


def test_duplicate_username_is_rejected(client):
    body = {"username": "twice", "password": "Password123!"}
    assert client.post("/auth/signup", json=body).status_code == 201
    dupe = client.post("/auth/signup", json=body)
    assert dupe.status_code == 400
    assert "already registered" in dupe.json()["error"]["message"]


def test_every_failure_uses_the_same_error_envelope(client, make_dev):
    dev = make_dev("envelope")
    failures = [
        client.get("/definitely-not-a-code", follow_redirects=False),          # 404
        client.post("/shorten", json={"url": "not-a-url"}, headers=dev.api),   # 422
        client.get("/urls"),                                                    # 401
    ]

    for res in failures:
        body = res.json()
        assert set(body) == {"error"}, body
        assert body["error"]["code"] == res.status_code
        assert "message" in body["error"]


def test_redact_dict_masks_secrets_at_any_nesting_depth():
    payload = {
        "username": "visible",
        "password": "hunter2",
        "session": {"access_token": "jwt-value", "keys": [{"plain_key": "sk_live_abc"}]},
    }

    safe = redact_dict(payload)

    assert safe["username"] == "visible"
    assert safe["password"] == "[REDACTED]"
    assert safe["session"]["access_token"] == "[REDACTED]"
    assert safe["session"]["keys"][0]["plain_key"] == "[REDACTED]"


def test_oauth_state_must_be_issued_by_us(client):
    """
    Regression: validation used to fall back to `len(state) >= 20`, so any long string
    an attacker invented passed the CSRF check — i.e. no CSRF protection at all.
    """
    forged = "x" * 64
    res = client.get(f"/auth/google/callback?code=abc&state={forged}")

    assert res.status_code == 400
    assert "CSRF" in res.json()["error"]["message"]


def test_oauth_state_is_single_use(client):
    """A replayed callback must fail even though the state was genuinely issued."""
    state = client.get("/auth/google/login").json()["state"]

    with patch(
        "app.services.auth_service.fetch_google_user_profile",
        return_value={"email": "replay@example.com", "sub": "sub_replay"},
    ):
        first = client.get(f"/auth/google/callback?code=abc&state={state}")
        replay = client.get(f"/auth/google/callback?code=abc&state={state}")

    assert first.status_code == 200
    assert replay.status_code == 400


def test_oauth_state_is_stored_out_of_process(client, redis_stub):
    """In-process dicts do not survive a restart or a second worker; Redis keys do."""
    state = client.get("/auth/google/login").json()["state"]
    assert f"oauth_state:{state}" in redis_stub.states.keys


def test_custom_alias_format_rules_are_enforced(client, make_dev):
    dev = make_dev("aliasrules")
    for bad in ["ab", "has spaces", "emoji🔥", "x" * 51]:
        res = client.post(
            "/shorten",
            json={"url": "https://example.com/a", "custom_alias": bad},
            headers=dev.api,
        )
        assert res.status_code == 400, f"alias {bad!r} should be rejected"
