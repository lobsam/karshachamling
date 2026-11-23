"""Dashboard numbers, contact messages and donations for /admin."""

import csv
import io
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response

from app import schemas
from app.context import Ctx, get_ctx
from app.db import now_iso
from app.security import get_current_user, require_admin

router = APIRouter(prefix="/admin", tags=["admin"])


async def _count(ctx: Ctx, sql: str, *params) -> int:
    return (await ctx.db.first(sql, *params))["n"] or 0


@router.get("/dashboard", response_model=schemas.DashboardOut)
async def dashboard(ctx: Ctx = Depends(get_ctx), user: dict = Depends(get_current_user)):
    out = schemas.DashboardOut(
        news_published=await _count(
            ctx, "SELECT COUNT(*) AS n FROM news WHERE status = 'published'"
        ),
        news_drafts=await _count(ctx, "SELECT COUNT(*) AS n FROM news WHERE status = 'draft'"),
        upcoming_events=await _count(
            ctx, "SELECT COUNT(*) AS n FROM events WHERE COALESCE(end_at, start_at) >= ?", now_iso()
        ),
        guide_sections=await _count(ctx, "SELECT COUNT(*) AS n FROM guide_sections"),
        media_count=await _count(ctx, "SELECT COUNT(*) AS n FROM media"),
        unread_messages=await _count(
            ctx, "SELECT COUNT(*) AS n FROM contact_messages WHERE is_read = 0"
        ),
    )
    if user["role"] == "admin":  # donor details are admin-only
        row = await ctx.db.first(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount_paise), 0) AS total "
            "FROM donations WHERE status = 'paid'"
        )
        out.donations_paid_count = row["n"]
        out.donations_paid_total_paise = row["total"]
    return out


# ---------- Contact messages (admins + editors) ----------


@router.get("/messages", response_model=schemas.Paginated[schemas.ContactOut])
async def list_messages(
    unread: bool = False,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    ctx: Ctx = Depends(get_ctx),
    _=Depends(get_current_user),
):
    where = "is_read = 0" if unread else "1 = 1"
    total = await _count(ctx, f"SELECT COUNT(*) AS n FROM contact_messages WHERE {where}")
    rows = await ctx.db.all(
        f"SELECT * FROM contact_messages WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        limit, (page - 1) * limit,
    )  # fmt: skip
    return {"items": rows, "total": total}


@router.patch("/messages/{msg_id}", response_model=schemas.ContactOut)
async def update_message(
    msg_id: int,
    data: schemas.ContactUpdate,
    ctx: Ctx = Depends(get_ctx),
    _=Depends(get_current_user),
):
    row = await ctx.db.first(
        "UPDATE contact_messages SET is_read = ?, updated_at = ? WHERE id = ? RETURNING *",
        data.is_read, now_iso(), msg_id,
    )  # fmt: skip
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return row


@router.delete("/messages/{msg_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_message(msg_id: int, ctx: Ctx = Depends(get_ctx), _=Depends(get_current_user)):
    if not await ctx.db.run("DELETE FROM contact_messages WHERE id = ?", msg_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


# ---------- Donations (admins only) ----------


def _donation_filter(status_: str | None) -> tuple[str, list]:
    return ("status = ?", [status_]) if status_ else ("1 = 1", [])


@router.get("/donations", response_model=schemas.Paginated[schemas.DonationOut])
async def list_donations(
    status_: str | None = Query("paid", alias="status"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    ctx: Ctx = Depends(get_ctx),
    _=Depends(require_admin),
):
    where, params = _donation_filter(status_)
    total = await _count(ctx, f"SELECT COUNT(*) AS n FROM donations WHERE {where}", *params)
    rows = await ctx.db.all(
        f"SELECT * FROM donations WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        *params, limit, (page - 1) * limit,
    )  # fmt: skip
    return {"items": rows, "total": total}


@router.get("/donations/export.csv")
async def export_donations(
    status_: str | None = Query("paid", alias="status"),
    ctx: Ctx = Depends(get_ctx),
    _=Depends(require_admin),
):
    where, params = _donation_filter(status_)
    rows = await ctx.db.all(f"SELECT * FROM donations WHERE {where} ORDER BY id", *params)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "id", "date", "status", "amount_inr", "name", "email", "phone", "pan", "address",
        "message", "razorpay_order_id", "razorpay_payment_id", "source",
    ])  # fmt: skip
    for d in rows:
        w.writerow([
            d["id"], d["paid_at"] or d["created_at"], d["status"], d["amount_paise"] / 100,
            d["donor_name"], d["donor_email"], d["donor_phone"], d["donor_pan"],
            d["donor_address"], d["message"], d["razorpay_order_id"] or "",
            d["razorpay_payment_id"] or "", d["source"],
        ])  # fmt: skip
    filename = f"donations-{datetime.now(UTC):%Y-%m-%d}.csv"
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
