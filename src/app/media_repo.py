"""Turning media rows into API output, and attaching cover images to content rows."""

import json

from app.db import Database


def public_url(base_url: str, key: str) -> str:
    return f"{base_url.rstrip('/')}/{key}"


def media_out(row: dict, base_url: str) -> dict:
    variants = row["variants"]
    if isinstance(variants, str):
        variants = json.loads(variants or "{}")
    urls = {k: public_url(base_url, v) for k, v in variants.items()}
    urls["original"] = public_url(base_url, row["original_key"])
    return {
        **{k: row[k] for k in (
            "id", "filename", "mime_type", "width", "height", "size_bytes", "alt", "alt_bo",
            "source_url", "created_at",
        )},
        "url": urls.get("md", urls["original"]),
        "urls": urls,
    }  # fmt: skip


async def attach_covers(db: Database, rows: list[dict], base_url: str) -> list[dict]:
    """Add a `cover_image` dict to each row with one extra query."""
    ids = sorted({r["cover_image_id"] for r in rows if r.get("cover_image_id")})
    media: dict[int, dict] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for m in await db.all(f"SELECT * FROM media WHERE id IN ({marks})", *ids):
            media[m["id"]] = media_out(m, base_url)
    for r in rows:
        r["cover_image"] = media.get(r.get("cover_image_id"))
    return rows
