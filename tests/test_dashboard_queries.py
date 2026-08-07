"""Dashboard list API: pagination, filtering, sorting, ownership scoping, retargeting."""
from app.models import URLModel, UserModel


def _seed(client, dev, db_session, clicks):
    """Create one link per click-count and backfill the counter the API never sets."""
    codes = []
    for n in clicks:
        code = client.post(
            "/shorten", json={"url": f"https://example.com/{n}"}, headers=dev.api
        ).json()["short_code"]
        db_session.query(URLModel).filter_by(short_code=code).update({"clicks": n})
        codes.append(code)
    db_session.commit()
    return codes


def test_pagination_reports_total_independent_of_the_page(client, make_dev, db_session):
    dev = make_dev("pager")
    _seed(client, dev, db_session, [0, 0, 0, 0, 0])

    page = client.get("/urls?skip=0&limit=2", headers=dev.jwt).json()
    tail = client.get("/urls?skip=4&limit=2", headers=dev.jwt).json()

    assert len(page["items"]) == 2 and page["total_count"] == 5
    assert len(tail["items"]) == 1 and tail["total_count"] == 5
    assert tail["skip"] == 4


def test_sorting_and_min_clicks_filter(client, make_dev, db_session):
    dev = make_dev("sorter")
    _seed(client, dev, db_session, [1, 50, 7])

    desc = client.get("/urls?sort_by=clicks&order=desc", headers=dev.jwt).json()["items"]
    asc = client.get("/urls?sort_by=clicks&order=asc", headers=dev.jwt).json()["items"]
    filtered = client.get("/urls?min_clicks=7", headers=dev.jwt).json()

    assert [u["clicks"] for u in desc] == [50, 7, 1]
    assert [u["clicks"] for u in asc] == [1, 7, 50]
    assert filtered["total_count"] == 2
    assert all(u["clicks"] >= 7 for u in filtered["items"])


def test_invalid_query_parameters_are_rejected(client, make_dev):
    """Regression: an unvalidated sort_by reached getattr(URLModel, ...) and 500'd."""
    dev = make_dev("badquery")
    assert client.get("/urls?limit=500", headers=dev.jwt).status_code == 422
    assert client.get("/urls?skip=-1", headers=dev.jwt).status_code == 422
    for injected in ["password_hash", "metadata", "registry", "owner"]:
        assert client.get(f"/urls?sort_by={injected}", headers=dev.jwt).status_code == 422
    assert client.get("/urls?order=sideways", headers=dev.jwt).status_code == 422


def test_list_is_scoped_to_the_owner_and_admins_see_everything(client, make_dev, db_session):
    alice = make_dev("alice")
    bob = make_dev("bob")
    _seed(client, alice, db_session, [0, 0])
    _seed(client, bob, db_session, [0])

    assert client.get("/urls", headers=alice.jwt).json()["total_count"] == 2
    assert client.get("/urls", headers=bob.jwt).json()["total_count"] == 1

    db_session.query(UserModel).filter_by(username="bob").update({"role": "admin"})
    db_session.commit()
    assert client.get("/urls", headers=bob.jwt).json()["total_count"] == 3


def test_soft_deleted_links_leave_the_list_but_keep_their_row(client, make_dev, db_session):
    dev = make_dev("softdel")
    (code,) = _seed(client, dev, db_session, [9])

    client.delete(f"/urls/{code}", headers=dev.jwt)

    assert client.get("/urls", headers=dev.jwt).json()["total_count"] == 0
    row = db_session.query(URLModel).filter_by(short_code=code).one()
    assert row.deleted_at is not None and row.clicks == 9  # analytics survive


def test_retarget_updates_the_row_and_refreshes_the_cache(client, make_dev, redis_stub):
    dev = make_dev("retarget")
    code = client.post(
        "/shorten", json={"url": "https://example.com/old"}, headers=dev.api
    ).json()["short_code"]
    redis_stub.cache_set.reset_mock()

    res = client.patch(
        f"/urls/{code}",
        json={"new_original_url": "https://example.com/new"},
        headers=dev.jwt,
    )

    assert res.status_code == 200
    assert res.json()["original_url"] == "https://example.com/new"
    assert redis_stub.cache_set.call_args[0][:2] == (code, "https://example.com/new")


def test_mutating_a_missing_link_is_404_not_403(client, make_dev):
    dev = make_dev("missing")
    patched = client.patch(
        "/urls/ghost", json={"new_original_url": "https://example.com/x"}, headers=dev.jwt
    )
    assert patched.status_code == 404
    assert client.delete("/urls/ghost", headers=dev.jwt).status_code == 404
