from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app import schemas
from app.context import Ctx, get_ctx
from app.db import now_iso
from app.security import (
    check_login_allowed,
    clear_login_failures,
    clear_session_cookie,
    create_session_token,
    get_current_user,
    hash_password,
    record_login_failure,
    require_admin,
    set_session_cookie,
    verify_password,
)

router = APIRouter(tags=["auth"])

_dummy_hash: str | None = None


def _timing_hash() -> str:
    """A real hash to verify against when the email doesn't exist (same response time)."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("dummy-password-for-timing")
    return _dummy_hash


def _client_ip(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "-"
    )


@router.post("/auth/login", response_model=schemas.UserOut)
async def login(
    data: schemas.LoginIn, request: Request, response: Response, ctx: Ctx = Depends(get_ctx)
):
    email = data.email.lower()
    key = f"{_client_ip(request)}:{email}"
    await check_login_allowed(ctx, key)
    user = await ctx.db.first("SELECT * FROM users WHERE email = ?", email)
    ok = verify_password(user["password_hash"] if user else _timing_hash(), data.password)
    if not user or not ok or not user["is_active"]:
        await record_login_failure(ctx, key)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password")
    await clear_login_failures(ctx, key)
    user = await ctx.db.first(
        "UPDATE users SET last_login_at = ? WHERE id = ? RETURNING *", now_iso(), user["id"]
    )
    set_session_cookie(ctx, response, create_session_token(ctx, user))
    return user


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response, ctx: Ctx = Depends(get_ctx)):
    clear_session_cookie(ctx, response)


@router.get("/auth/me", response_model=schemas.UserOut)
async def me(user: dict = Depends(get_current_user)):
    return user


@router.post("/auth/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    data: schemas.PasswordChange,
    response: Response,
    ctx: Ctx = Depends(get_ctx),
    user: dict = Depends(get_current_user),
):
    if not verify_password(user["password_hash"], data.current_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is wrong")
    user = await ctx.db.first(
        "UPDATE users SET password_hash = ?, token_version = token_version + 1, updated_at = ? "
        "WHERE id = ? RETURNING *",  # bumping token_version signs out other devices
        hash_password(data.new_password), now_iso(), user["id"],
    )  # fmt: skip
    set_session_cookie(ctx, response, create_session_token(ctx, user))


# ---------- User management (admins only) ----------


@router.get("/admin/users", response_model=list[schemas.UserOut])
async def list_users(ctx: Ctx = Depends(get_ctx), _=Depends(require_admin)):
    return await ctx.db.all("SELECT * FROM users ORDER BY created_at, id")


@router.post("/admin/users", response_model=schemas.UserOut, status_code=201)
async def create_user(
    data: schemas.UserCreate, ctx: Ctx = Depends(get_ctx), _=Depends(require_admin)
):
    email = data.email.lower()
    if await ctx.db.first("SELECT 1 FROM users WHERE email = ?", email):
        raise HTTPException(status.HTTP_409_CONFLICT, "A user with this email already exists")
    return await ctx.db.first(
        "INSERT INTO users (email, name, role, password_hash) VALUES (?, ?, ?, ?) RETURNING *",
        email, data.name, data.role, hash_password(data.password),
    )  # fmt: skip


@router.patch("/admin/users/{user_id}", response_model=schemas.UserOut)
async def update_user(
    user_id: int,
    data: schemas.UserUpdate,
    ctx: Ctx = Depends(get_ctx),
    me_: dict = Depends(require_admin),
):
    user = await ctx.db.first("SELECT * FROM users WHERE id = ?", user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if user_id == me_["id"] and (data.role == "editor" or data.is_active is False):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot demote or disable yourself")
    name = data.name if data.name is not None else user["name"]
    role = data.role or user["role"]
    active = data.is_active if data.is_active is not None else bool(user["is_active"])
    pw_hash = hash_password(data.password) if data.password else user["password_hash"]
    # Disabling a user or resetting their password signs them out everywhere
    bump = int(bool(data.password) or (user["is_active"] and not active))
    return await ctx.db.first(
        "UPDATE users SET name = ?, role = ?, is_active = ?, password_hash = ?, "
        "token_version = token_version + ?, updated_at = ? WHERE id = ? RETURNING *",
        name, role, active, pw_hash, bump, now_iso(), user_id,
    )  # fmt: skip


@router.delete("/admin/users/{user_id}", status_code=204)
async def delete_user(
    user_id: int, ctx: Ctx = Depends(get_ctx), me_: dict = Depends(require_admin)
):
    if user_id == me_["id"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot delete yourself")
    if not await ctx.db.run("DELETE FROM users WHERE id = ?", user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
