import hashlib
import hmac
import io
import json

from fastapi.testclient import TestClient
from PIL import Image

from app import http
from app.main import app


def png_bytes(w=2400, h=1600) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (180, 40, 40)).save(buf, "PNG")
    return buf.getvalue()


def jpeg_bytes(w=4000, h=3000) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (20, 90, 160)).save(buf, "JPEG", quality=85)
    return buf.getvalue()


# ---------- Auth ----------


def test_login_wrong_password_and_rate_limit(client):
    for _ in range(5):
        r = client.post("/auth/login", json={"email": "admin@example.org", "password": "nope"})
        assert r.status_code == 401
    r = client.post("/auth/login", json={"email": "admin@example.org", "password": "nope"})
    assert r.status_code == 429


def test_admin_routes_need_login(client):
    assert client.get("/admin/news").status_code == 401
    assert client.get("/auth/me").status_code == 401


def test_mutations_need_csrf_header(admin):
    plain = TestClient(app, cookies=admin.cookies)
    assert plain.post("/admin/news", json={"title": "x"}).status_code == 403


def test_editor_cannot_see_donations_or_users(editor):
    assert editor.get("/admin/donations").status_code == 403
    assert editor.get("/admin/users").status_code == 403
    assert editor.get("/admin/dashboard").json()["donations_paid_count"] is None


def test_password_change_signs_out_other_sessions(admin):
    other = TestClient(app, cookies=dict(admin.cookies))
    r = admin.post(
        "/auth/password",
        json={"current_password": "correct-horse-battery", "new_password": "a-brand-new-pass"},
    )
    assert r.status_code == 204
    assert admin.get("/auth/me").status_code == 200  # this session got a fresh cookie
    assert other.get("/auth/me").status_code == 401  # the old token is dead


def test_cors_allows_only_configured_origin(client):
    pre = {"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"}
    r = client.options("/admin/news", headers=pre)
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert r.headers["access-control-allow-credentials"] == "true"
    r = client.options("/admin/news", headers={**pre, "Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


# ---------- Content ----------


def test_news_draft_hidden_until_published(admin, client):
    r = admin.post(
        "/admin/news",
        json={"title": "Losar Celebrations 2027", "body": "<p>Hi</p><script>alert(1)</script>"},
    )
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["slug"] == "losar-celebrations-2027"
    assert "<script>" not in item["body"]
    assert client.get("/news").json()["total"] == 0
    assert client.get("/news/losar-celebrations-2027").status_code == 404

    item["status"] = "published"
    r = admin.put(f"/admin/news/{item['id']}", json=item)
    assert r.status_code == 200, r.text
    assert r.json()["published_at"] is not None
    assert client.get("/news").json()["total"] == 1
    assert client.get("/news/losar-celebrations-2027").json()["title"] == "Losar Celebrations 2027"


def test_duplicate_titles_get_unique_slugs(admin):
    a = admin.post("/admin/news", json={"title": "Same"}).json()
    b = admin.post("/admin/news", json={"title": "Same"}).json()
    assert (a["slug"], b["slug"]) == ("same", "same-2")
    # Editing keeps the item's own slug
    b["title"] = "Same"
    assert admin.put(f"/admin/news/{b['id']}", json=b).json()["slug"] == "same-2"


def test_tibetan_fields(admin, client):
    admin.post(
        "/admin/pages",
        json={"slug": "about", "title": "About", "title_bo": "ངོ་སྤྲོད།", "status": "published"},
    )
    assert client.get("/pages/about").json()["title_bo"] == "ངོ་སྤྲོད།"


def test_events_upcoming_and_past(admin, client):
    for title, start in (("Gustor Festival", "2099-01-10T09:00:00+05:30"),
                         ("Old Puja", "2020-01-10T09:00:00+05:30")):  # fmt: skip
        r = admin.post(
            "/admin/events", json={"title": title, "start_at": start, "status": "published"}
        )
        assert r.status_code == 201, r.text
    assert r.json()["start_at"] == "2020-01-10T03:30:00Z"  # stored in UTC
    assert [e["title"] for e in client.get("/events").json()["items"]] == ["Gustor Festival"]
    assert [e["title"] for e in client.get("/events?when=past").json()["items"]] == ["Old Puja"]

    r = admin.post(
        "/admin/events",
        json={"title": "Bad", "start_at": "2099-01-10T09:00:00Z", "end_at": "2099-01-09T09:00:00Z"},
    )
    assert r.status_code == 422


def test_guide_order_and_reorder(admin, client):
    ids = [
        admin.post("/admin/guide", json={"title": t, "status": "published"}).json()["id"]
        for t in ("How to reach", "Best time to visit", "Etiquette")
    ]
    titles = lambda: [s["title"] for s in client.get("/guide").json()]  # noqa: E731
    assert titles() == ["How to reach", "Best time to visit", "Etiquette"]
    assert (
        admin.post("/admin/guide/reorder", json={"ids": [ids[2], ids[0], ids[1]]}).status_code
        == 204
    )
    assert titles() == ["Etiquette", "How to reach", "Best time to visit"]
    assert admin.post("/admin/guide/reorder", json={"ids": ids[:2]}).status_code == 422


def test_deleting_content(admin):
    item = admin.post("/admin/news", json={"title": "Temp"}).json()
    assert admin.delete(f"/admin/news/{item['id']}").status_code == 204
    assert admin.get(f"/admin/news/{item['id']}").status_code == 404


# ---------- Media ----------


def test_upload_creates_webp_sizes_and_cover_image(admin, client, ctx):
    r = admin.post(
        "/admin/media",
        files={"file": ("Karsha Gompa.png", png_bytes(), "image/png")},
        data={"alt": "Karsha Gompa"},
    )
    assert r.status_code == 201, r.text
    media = r.json()
    assert (media["width"], media["height"], media["alt"]) == (2400, 1600, "Karsha Gompa")
    assert set(media["urls"]) == {"sm", "md", "lg", "original"}
    assert media["urls"]["md"].endswith("/1200.webp")
    assert len(ctx.storage.files) == 4  # original + 400 + 1200 + 2000

    # The stored file is served from R2 and is really 1200px wide
    path = media["urls"]["md"].split("/media/", 1)[1]
    img = Image.open(io.BytesIO(client.get(f"/media/{path}").content))
    assert img.size == (1200, 800)

    news = admin.post(
        "/admin/news",
        json={"title": "With cover", "cover_image_id": media["id"], "status": "published"},
    ).json()
    assert news["cover_image"]["url"] == media["url"]
    assert client.get("/news").json()["items"][0]["cover_image"]["id"] == media["id"]

    # In use → protected from accidental deletion
    r = admin.delete(f"/admin/media/{media['id']}")
    assert r.status_code == 409
    assert "news: With cover" in r.json()["detail"]["used_in"]
    assert admin.delete(f"/admin/media/{media['id']}?force=true").status_code == 204
    assert ctx.storage.files == {}
    assert admin.get(f"/admin/news/{news['id']}").json()["cover_image"] is None


def test_big_jpeg_is_downscaled(admin):
    r = admin.post("/admin/media", files={"file": ("phone.jpg", jpeg_bytes(), "image/jpeg")})
    assert r.status_code == 201, r.text
    m = r.json()
    assert (m["width"], m["height"]) == (4000, 3000)
    assert m["urls"]["lg"].endswith("/2000.webp")


def test_small_image_reuses_one_variant(admin, ctx):
    r = admin.post("/admin/media", files={"file": ("icon.png", png_bytes(300, 200), "image/png")})
    m = r.json()
    assert m["urls"]["sm"] == m["urls"]["md"] == m["urls"]["lg"]
    assert len(ctx.storage.files) == 2  # original + one 300px WebP


def test_upload_rejects_non_images(admin):
    r = admin.post("/admin/media", files={"file": ("x.png", b"not an image", "image/png")})
    assert r.status_code == 422


# ---------- Donations ----------


def _sign(secret: str, data: bytes) -> str:
    return hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()


def test_donation_flow(admin, client, monkeypatch):
    calls = []

    async def fake_post(url, **kw):
        calls.append((url, kw))
        return http.HttpResponse(200, json.dumps({"id": "order_TEST123"}))

    monkeypatch.setattr(http, "post", fake_post)
    r = client.post(
        "/donations/order",
        json={
            "amount_inr": 501,
            "name": "Tenzin",
            "email": "tenzin@example.org",
            "pan": "abcde1234f",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json() == {
        "order_id": "order_TEST123", "key_id": "rzp_test_key", "amount_paise": 50100,
        "currency": "INR",
    }  # fmt: skip
    assert calls[0][1]["json_body"]["amount"] == 50100
    assert calls[0][1]["basic_auth"] == ("rzp_test_key", "test_key_secret")

    bad = {
        "razorpay_order_id": "order_TEST123",
        "razorpay_payment_id": "pay_1",
        "razorpay_signature": "0" * 64,
    }
    assert client.post("/donations/verify", json=bad).status_code == 400
    good = {**bad, "razorpay_signature": _sign("test_key_secret", b"order_TEST123|pay_1")}
    assert client.post("/donations/verify", json=good).json()["amount_paise"] == 50100

    body = json.dumps({
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": "pay_1", "order_id": "order_TEST123"}}},
    }).encode()  # fmt: skip
    sig = _sign("test_webhook_secret", body)
    r = client.post("/donations/webhook", content=body, headers={"x-razorpay-signature": sig})
    assert r.status_code == 200
    r = client.post("/donations/webhook", content=body, headers={"x-razorpay-signature": "bad"})
    assert r.status_code == 400

    donations = admin.get("/admin/donations").json()
    assert donations["total"] == 1
    assert donations["items"][0]["donor_pan"] == "ABCDE1234F"
    assert admin.get("/admin/dashboard").json()["donations_paid_total_paise"] == 50100
    csv = admin.get("/admin/donations/export.csv")
    assert "Tenzin" in csv.text and csv.headers["content-type"].startswith("text/csv")


def test_razorpay_error_is_reported(client, monkeypatch):
    async def failing_post(url, **kw):
        return http.HttpResponse(401, '{"error": "bad key"}')

    monkeypatch.setattr(http, "post", failing_post)
    r = client.post("/donations/order", json={"amount_inr": 100, "name": "A", "email": "a@b.org"})
    assert r.status_code == 502


def test_minimum_donation(client):
    r = client.post("/donations/order", json={"amount_inr": 1, "name": "A", "email": "a@b.org"})
    assert r.status_code == 422


# ---------- Contact ----------


def test_contact_message_inbox(client, editor):
    r = client.post(
        "/contact", json={"name": "Visitor", "email": "v@example.org", "message": "March visit?"}
    )
    assert r.status_code == 201
    assert editor.get("/admin/dashboard").json()["unread_messages"] == 1
    msg = editor.get("/admin/messages").json()["items"][0]
    assert editor.patch(f"/admin/messages/{msg['id']}", json={"is_read": True}).json()["is_read"]
    assert editor.get("/admin/dashboard").json()["unread_messages"] == 0
    assert editor.delete(f"/admin/messages/{msg['id']}").status_code == 204


# ---------- Users ----------


def test_admin_manages_users(admin):
    r = admin.post(
        "/admin/users",
        json={"email": "Monk@Example.org", "name": "Monk", "password": "long-enough-pw"},
    )
    assert r.status_code == 201
    monk = r.json()
    assert monk["email"] == "monk@example.org"
    dup = {"email": "monk@example.org", "password": "long-enough-pw"}
    assert admin.post("/admin/users", json=dup).status_code == 409
    assert (
        admin.patch(f"/admin/users/{monk['id']}", json={"role": "admin"}).json()["role"] == "admin"
    )

    me = admin.get("/auth/me").json()
    assert admin.patch(f"/admin/users/{me['id']}", json={"role": "editor"}).status_code == 400
    assert admin.delete(f"/admin/users/{me['id']}").status_code == 400
    assert admin.delete(f"/admin/users/{monk['id']}").status_code == 204


def test_disabled_user_is_signed_out(admin):
    admin.post("/admin/users", json={"email": "e2@example.org", "password": "long-enough-pw"})
    c = TestClient(app, headers={"x-requested-with": "kc-admin"})
    c.post("/auth/login", json={"email": "e2@example.org", "password": "long-enough-pw"})
    assert c.get("/auth/me").status_code == 200
    uid = c.get("/auth/me").json()["id"]
    admin.patch(f"/admin/users/{uid}", json={"is_active": False})
    assert c.get("/auth/me").status_code == 401


def test_docs_hidden_in_production_unless_enabled(ctx, client):
    ctx.settings.env = "production"
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    ctx.settings.enable_docs = True
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
