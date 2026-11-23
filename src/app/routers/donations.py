"""Razorpay donations: create order → donor pays in Checkout.js → verify / webhook."""

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app import http, schemas
from app.context import Ctx, get_ctx
from app.db import now_iso

router = APIRouter(prefix="/donations", tags=["donations"])
log = logging.getLogger(__name__)
RAZORPAY_API = "https://api.razorpay.com/v1"


def _hmac_ok(secret: str, message: bytes, signature: str) -> bool:
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


async def _mark_paid(ctx: Ctx, order_id: str, payment_id: str) -> dict | None:
    await ctx.db.run(
        "UPDATE donations SET status = 'paid', razorpay_payment_id = ?, paid_at = ?, "
        "updated_at = ? WHERE razorpay_order_id = ? AND status != 'paid'",
        payment_id, now_iso(), now_iso(), order_id,
    )  # fmt: skip
    return await ctx.db.first("SELECT * FROM donations WHERE razorpay_order_id = ?", order_id)


@router.post("/order", response_model=schemas.DonationOrderOut, status_code=201)
async def create_order(data: schemas.DonationOrderIn, ctx: Ctx = Depends(get_ctx)):
    s = ctx.settings
    if not (s.razorpay_key_id and s.razorpay_key_secret):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Online donations are not set up")
    if data.amount_inr < s.min_donation_inr:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Minimum donation is ₹{s.min_donation_inr}"
        )
    amount_paise = data.amount_inr * 100
    try:
        r = await http.post(
            f"{RAZORPAY_API}/orders",
            basic_auth=(s.razorpay_key_id, s.razorpay_key_secret),
            json_body={
                "amount": amount_paise,
                "currency": "INR",
                "notes": {"purpose": "donation", "donor": data.name[:200]},
            },
        )
    except http.HttpError as e:
        log.exception("Razorpay order creation failed")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Payment provider error, please retry"
        ) from e
    if not r.ok:
        log.error("Razorpay returned %s: %s", r.status, r.text[:300])
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Payment provider error, please retry")
    order_id = r.json()["id"]
    await ctx.db.run(
        "INSERT INTO donations (razorpay_order_id, amount_paise, currency, donor_name, "
        "donor_email, donor_phone, donor_pan, donor_address, message) "
        "VALUES (?, ?, 'INR', ?, ?, ?, ?, ?, ?)",
        order_id, amount_paise, data.name, data.email, data.phone, data.pan.upper(),
        data.address, data.message,
    )  # fmt: skip
    return {
        "order_id": order_id,
        "key_id": s.razorpay_key_id,
        "amount_paise": amount_paise,
        "currency": "INR",
    }


@router.post("/verify")
async def verify_payment(data: schemas.DonationVerifyIn, ctx: Ctx = Depends(get_ctx)):
    """Called by the browser after Checkout.js succeeds, to show a thank-you page."""
    secret = ctx.settings.razorpay_key_secret
    msg = f"{data.razorpay_order_id}|{data.razorpay_payment_id}".encode()
    if not secret or not _hmac_ok(secret, msg, data.razorpay_signature):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Payment could not be verified")
    donation = await _mark_paid(ctx, data.razorpay_order_id, data.razorpay_payment_id)
    if not donation:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Donation not found")
    return {"ok": True, "amount_paise": donation["amount_paise"], "name": donation["donor_name"]}


@router.post("/webhook")
async def webhook(request: Request, ctx: Ctx = Depends(get_ctx)):
    """Razorpay server-to-server events. This is the source of truth for payment status."""
    secret = ctx.settings.razorpay_webhook_secret
    raw = await request.body()
    if not secret or not _hmac_ok(secret, raw, request.headers.get("x-razorpay-signature", "")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Bad signature")
    event = json.loads(raw)
    payment = event.get("payload", {}).get("payment", {}).get("entity", {})
    order_id, payment_id = payment.get("order_id"), payment.get("id")
    if event.get("event") in ("payment.captured", "order.paid") and order_id:
        await _mark_paid(ctx, order_id, payment_id)
    elif event.get("event") == "payment.failed" and order_id:
        await ctx.db.run(
            "UPDATE donations SET status = 'failed', updated_at = ? "
            "WHERE razorpay_order_id = ? AND status = 'created'",
            now_iso(), order_id,
        )  # fmt: skip
    return {"ok": True}
