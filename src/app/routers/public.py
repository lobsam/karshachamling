"""Read-only endpoints for the public website (published content only), plus the contact form."""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response

from app import http, schemas
from app.context import Ctx, get_ctx
from app.db import now_iso
from app.media_repo import attach_covers

router = APIRouter(tags=["public"])
log = logging.getLogger(__name__)


async def _list(ctx: Ctx, table: str, where: str, params: list, order: str, page: int, limit: int):
    w = f"status = 'published' AND {where}"
    total = await ctx.db.first(f"SELECT COUNT(*) AS n FROM {table} WHERE {w}", *params)
    rows = await ctx.db.all(
        f"SELECT * FROM {table} WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?",
        *params, limit, (page - 1) * limit,
    )  # fmt: skip
    items = await attach_covers(ctx.db, rows, ctx.settings.media_public_base_url)
    return {"items": items, "total": total["n"]}


async def _one(ctx: Ctx, table: str, slug: str) -> dict:
    row = await ctx.db.first(f"SELECT * FROM {table} WHERE slug = ? AND status = 'published'", slug)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return (await attach_covers(ctx.db, [row], ctx.settings.media_public_base_url))[0]


@router.get("/news", response_model=schemas.Paginated[schemas.NewsOut])
async def list_news(
    page: int = Query(1, ge=1), limit: int = Query(12, ge=1, le=100), ctx: Ctx = Depends(get_ctx)
):
    return await _list(ctx, "news", "1 = 1", [], "published_at DESC, id DESC", page, limit)


@router.get("/news/{slug}", response_model=schemas.NewsOut)
async def get_news(slug: str, ctx: Ctx = Depends(get_ctx)):
    return await _one(ctx, "news", slug)


@router.get("/events", response_model=schemas.Paginated[schemas.EventOut])
async def list_events(
    when: Literal["upcoming", "past", "all"] = "upcoming",
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    ctx: Ctx = Depends(get_ctx),
):
    # An event is "upcoming" until it has finished (end_at, or start_at if no end)
    ends = "COALESCE(end_at, start_at)"
    if when == "upcoming":
        return await _list(ctx, "events", f"{ends} >= ?", [now_iso()], "start_at ASC", page, limit)
    if when == "past":
        return await _list(ctx, "events", f"{ends} < ?", [now_iso()], "start_at DESC", page, limit)
    return await _list(ctx, "events", "1 = 1", [], "start_at DESC", page, limit)


@router.get("/events/{slug}", response_model=schemas.EventOut)
async def get_event(slug: str, ctx: Ctx = Depends(get_ctx)):
    return await _one(ctx, "events", slug)


@router.get("/guide", response_model=list[schemas.GuideOut])
async def get_guide(ctx: Ctx = Depends(get_ctx)):
    return (await _list(ctx, "guide_sections", "1 = 1", [], "position, id", 1, 200))["items"]


@router.get("/pages", response_model=list[schemas.PageOut])
async def list_pages(ctx: Ctx = Depends(get_ctx)):
    return (await _list(ctx, "pages", "1 = 1", [], "title", 1, 200))["items"]


@router.get("/pages/{slug}", response_model=schemas.PageOut)
async def get_page(slug: str, ctx: Ctx = Depends(get_ctx)):
    return await _one(ctx, "pages", slug)


@router.get("/media/{key:path}", include_in_schema=False)
async def serve_media(key: str, ctx: Ctx = Depends(get_ctx)):
    """Serve files from R2. In production images come from media.karshachamling.org instead."""
    found = await ctx.storage.get(key)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    data, content_type = found
    return Response(
        data, media_type=content_type, headers={"Cache-Control": "public, max-age=3600"}
    )


# ---------- Contact form ----------


async def _verify_turnstile(ctx: Ctx, token: str, ip: str | None) -> bool:
    secret = ctx.settings.turnstile_secret
    if not secret:
        return True
    try:
        r = await http.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            form={"secret": secret, "response": token, "remoteip": ip or ""},
        )
        return bool(r.json().get("success"))
    except (http.HttpError, ValueError):
        log.exception("Turnstile verification failed")
        return False


async def _notify_new_message(ctx: Ctx, msg: dict) -> None:
    s = ctx.settings
    if not (s.resend_api_key and s.email_from and s.notify_email):
        return
    try:
        r = await http.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {s.resend_api_key}"},
            json_body={
                "from": s.email_from,
                "to": [s.notify_email],
                "reply_to": msg["email"],
                "subject": f"[Website] {msg['subject'] or 'New message'} — {msg['name']}",
                "text": f"From: {msg['name']} <{msg['email']}>\n\n{msg['message']}\n\n"
                f"(Message #{msg['id']} in /admin/messages)",
            },
        )
        if not r.ok:
            log.error("Resend returned %s: %s", r.status, r.text[:300])
    except http.HttpError:
        log.exception("Could not send contact notification email")


@router.post("/contact", status_code=status.HTTP_201_CREATED)
async def contact(data: schemas.ContactIn, request: Request, ctx: Ctx = Depends(get_ctx)):
    ip = request.headers.get("cf-connecting-ip")
    if not await _verify_turnstile(ctx, data.turnstile_token, ip):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Spam check failed, please try again")
    msg = await ctx.db.first(
        "INSERT INTO contact_messages (name, email, subject, message) VALUES (?, ?, ?, ?) "
        "RETURNING *",
        data.name, data.email, data.subject, data.message,
    )  # fmt: skip
    await _notify_new_message(ctx, msg)
    return {"ok": True}
