"""Tests run on normal CPython: sqlite3 stands in for D1 (same SQL), a dict for R2."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.context import Ctx
from app.db import SqliteDatabase
from app.main import app
from app.security import CSRF_HEADER, CSRF_VALUE, hash_password
from app.storage import MemoryStorage

PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def ctx():
    c = Ctx(
        settings=Settings(
            jwt_secret="test-secret-that-is-at-least-32-bytes-long",
            media_public_base_url="http://testserver/media",
            razorpay_key_id="rzp_test_key",
            razorpay_key_secret="test_key_secret",
            razorpay_webhook_secret="test_webhook_secret",
            cors_origins="http://localhost:5173",
        ),
        db=SqliteDatabase(),
        storage=MemoryStorage(),
    )

    async def seed():
        for email, role in (("admin@example.org", "admin"), ("editor@example.org", "editor")):
            await c.db.run(
                "INSERT INTO users (email, name, role, password_hash) VALUES (?, ?, ?, ?)",
                email, role.title(), role, hash_password(PASSWORD),
            )  # fmt: skip

    asyncio.run(seed())
    app.state.test_ctx = c
    yield c


@pytest.fixture
def client():
    return TestClient(app)


def _login(email: str) -> TestClient:
    c = TestClient(app, headers={CSRF_HEADER: CSRF_VALUE})
    r = c.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def admin():
    return _login("admin@example.org")


@pytest.fixture
def editor():
    return _login("editor@example.org")
