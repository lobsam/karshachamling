from datetime import UTC, datetime, timedelta
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, Request, Response, status

from app.context import Ctx, get_ctx
from app.db import DT_FORMAT, now_iso

# Mutating admin requests must send this header. Browsers only allow it on
# cross-origin requests after a CORS preflight, which blocks CSRF from other sites.
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "kc-admin"

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW = timedelta(minutes=15)


@lru_cache
def _hasher():
    # Imported lazily: Workers forbid randomness while the startup snapshot is taken.
    # 19 MiB / 2 passes (OWASP minimum) keeps memory and CPU low inside the Worker.
    from argon2 import PasswordHasher

    return PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)


def hash_password(password: str) -> str:
    return _hasher().hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    from argon2.exceptions import InvalidHashError, VerificationError

    try:
        return _hasher().verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def create_session_token(ctx: Ctx, user: dict) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user["id"]),
        "ver": user["token_version"],
        "iat": now,
        "exp": now + timedelta(days=ctx.settings.session_days),
    }
    return jwt.encode(payload, ctx.settings.jwt_secret, algorithm="HS256")


def set_session_cookie(ctx: Ctx, response: Response, token: str) -> None:
    s = ctx.settings
    response.set_cookie(
        s.cookie_name,
        token,
        max_age=s.session_days * 86400,
        httponly=True,
        secure=s.is_production,
        samesite="lax",
        domain=s.cookie_domain,
        path="/",
    )


def clear_session_cookie(ctx: Ctx, response: Response) -> None:
    response.delete_cookie(ctx.settings.cookie_name, domain=ctx.settings.cookie_domain, path="/")


async def get_current_user(request: Request, ctx: Ctx = Depends(get_ctx)) -> dict:
    unsafe = request.method not in ("GET", "HEAD", "OPTIONS")
    if unsafe and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing CSRF header")
    token = request.cookies.get(ctx.settings.cookie_name)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    try:
        payload = jwt.decode(token, ctx.settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired") from e
    user = await ctx.db.first("SELECT * FROM users WHERE id = ?", int(payload["sub"]))
    if not user or not user["is_active"] or payload.get("ver") != user["token_version"]:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")
    return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admins only")
    return user


# ---------- Login rate limit (stored in D1 so it works across Worker isolates) ----------


def _window_start() -> str:
    return (datetime.now(UTC) - LOGIN_WINDOW).strftime(DT_FORMAT)


async def check_login_allowed(ctx: Ctx, key: str) -> None:
    row = await ctx.db.first(
        "SELECT COUNT(*) AS n FROM login_failures WHERE key = ? AND at >= ?", key, _window_start()
    )
    if row and row["n"] >= LOGIN_MAX_FAILURES:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts. Try again in 15 minutes."
        )


async def record_login_failure(ctx: Ctx, key: str) -> None:
    await ctx.db.batch(
        [
            ("INSERT INTO login_failures (key, at) VALUES (?, ?)", (key, now_iso())),
            ("DELETE FROM login_failures WHERE at < ?", (_window_start(),)),
        ]
    )


async def clear_login_failures(ctx: Ctx, key: str) -> None:
    await ctx.db.run("DELETE FROM login_failures WHERE key = ?", key)
