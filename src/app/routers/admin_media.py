import json

from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile, status

from app import schemas
from app.context import Ctx, get_ctx
from app.db import now_iso
from app.images import InvalidImage, process_and_store
from app.media_repo import media_out
from app.security import get_current_user

router = APIRouter(prefix="/admin/media", tags=["admin:media"])
CONTENT_TABLES = ("news", "events", "guide_sections", "pages")


async def _load(ctx: Ctx, media_id: int) -> dict:
    row = await ctx.db.first("SELECT * FROM media WHERE id = ?", media_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return row


@router.get("", response_model=schemas.Paginated[schemas.MediaOut])
async def list_media(
    q: str = "",
    source_url: str | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(60, ge=1, le=200),
    ctx: Ctx = Depends(get_ctx),
    _=Depends(get_current_user),
):
    where, params = ["1 = 1"], []
    if q:
        where.append("(filename LIKE ? OR alt LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if source_url:
        where.append("source_url = ?")
        params.append(source_url)
    w = " AND ".join(where)
    total = await ctx.db.first(f"SELECT COUNT(*) AS n FROM media WHERE {w}", *params)
    rows = await ctx.db.all(
        f"SELECT * FROM media WHERE {w} ORDER BY id DESC LIMIT ? OFFSET ?",
        *params, limit, (page - 1) * limit,
    )  # fmt: skip
    base = ctx.settings.media_public_base_url
    return {"items": [media_out(r, base) for r in rows], "total": total["n"]}


@router.post("", response_model=schemas.MediaOut, status_code=status.HTTP_201_CREATED)
async def upload_media(
    file: UploadFile,
    alt: str = Form(""),
    source_url: str | None = Form(None),  # set by the WordPress importer
    ctx: Ctx = Depends(get_ctx),
    user: dict = Depends(get_current_user),
):
    max_mb = ctx.settings.max_upload_mb
    data = await file.read(max_mb * 1024 * 1024 + 1)
    if len(data) > max_mb * 1024 * 1024:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, f"Image is larger than {max_mb} MB")
    if source_url and await ctx.db.first("SELECT 1 FROM media WHERE source_url = ?", source_url):
        raise HTTPException(status.HTTP_409_CONFLICT, "This file was already imported")
    try:
        stored = await process_and_store(data, file.filename or "image", ctx.storage)
    except InvalidImage as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    row = await ctx.db.first(
        "INSERT INTO media (filename, original_key, variants, mime_type, width, height, "
        "size_bytes, alt, source_url, uploaded_by_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "RETURNING *",
        file.filename or "image", stored.original_key, json.dumps(stored.variants),
        stored.mime_type, stored.width, stored.height, stored.size_bytes, alt, source_url,
        user["id"],
    )  # fmt: skip
    return media_out(row, ctx.settings.media_public_base_url)


@router.patch("/{media_id}", response_model=schemas.MediaOut)
async def update_media(
    media_id: int,
    data: schemas.MediaUpdate,
    ctx: Ctx = Depends(get_ctx),
    _=Depends(get_current_user),
):
    row = await _load(ctx, media_id)
    row = await ctx.db.first(
        "UPDATE media SET alt = ?, alt_bo = ?, updated_at = ? WHERE id = ? RETURNING *",
        data.alt if data.alt is not None else row["alt"],
        data.alt_bo if data.alt_bo is not None else row["alt_bo"],
        now_iso(), media_id,
    )  # fmt: skip
    return media_out(row, ctx.settings.media_public_base_url)


async def find_usages(ctx: Ctx, media: dict) -> list[str]:
    """Where is this image used? Checks cover images and images inside article bodies."""
    folder = "%" + media["original_key"].rsplit("/", 1)[0] + "/%"
    used: list[str] = []
    for table in CONTENT_TABLES:
        rows = await ctx.db.all(
            f"SELECT title FROM {table} WHERE cover_image_id = ? OR body LIKE ? OR body_bo LIKE ?",
            media["id"], folder, folder,
        )  # fmt: skip
        used += [f"{table}: {r['title']}" for r in rows]
    return used


@router.delete("/{media_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_media(
    media_id: int,
    force: bool = False,
    ctx: Ctx = Depends(get_ctx),
    _=Depends(get_current_user),
):
    media = await _load(ctx, media_id)
    usages = await find_usages(ctx, media)
    if usages and not force:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"message": "This image is still in use", "used_in": usages}
        )
    await ctx.db.run("DELETE FROM media WHERE id = ?", media_id)
    for key in {media["original_key"], *json.loads(media["variants"]).values()}:
        await ctx.storage.delete(key)  # files go only after the database row is gone
