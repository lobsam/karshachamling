"""Create / edit / delete for News, Events, Visitor Guide and Pages (login required)."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app import schemas
from app.context import Ctx, get_ctx
from app.db import now_iso, to_db_dt
from app.media_repo import attach_covers
from app.security import get_current_user
from app.utils import clean_html, slugify, trigger_site_rebuild

COMMON_FIELDS = [
    "slug", "title", "title_bo", "summary", "summary_bo", "body", "body_bo", "status",
    "cover_image_id",
]  # fmt: skip
DATETIME_FIELDS = {"published_at", "start_at", "end_at"}

Prepare = Callable[[Ctx, dict, dict | None], Awaitable[None]]


@dataclass
class Collection:
    path: str
    table: str
    in_schema: type[schemas.ContentIn]
    out_schema: type[schemas.ContentOut]
    order_by: str
    extra_fields: list[str] = field(default_factory=list)
    prepare: Prepare | None = None

    @property
    def fields(self) -> list[str]:
        return COMMON_FIELDS + self.extra_fields


async def _prepare_news(ctx: Ctx, values: dict, existing: dict | None) -> None:
    if values["status"] == "published" and not values.get("published_at"):
        values["published_at"] = (existing or {}).get("published_at") or now_iso()


async def _prepare_event(ctx: Ctx, values: dict, existing: dict | None) -> None:
    if values.get("end_at") and values["end_at"] < values["start_at"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "End must be after start")


async def _prepare_guide(ctx: Ctx, values: dict, existing: dict | None) -> None:
    if values.get("position") is None:
        if existing:
            values["position"] = existing["position"]
        else:
            row = await ctx.db.first("SELECT COALESCE(MAX(position), 0) AS p FROM guide_sections")
            values["position"] = row["p"] + 1


COLLECTIONS = [
    Collection(
        "news", "news", schemas.NewsIn, schemas.NewsOut,
        "published_at IS NOT NULL, published_at DESC, id DESC", ["published_at"], _prepare_news,
    ),
    Collection(
        "events", "events", schemas.EventIn, schemas.EventOut, "start_at DESC, id DESC",
        ["start_at", "end_at", "location", "location_bo"], _prepare_event,
    ),
    Collection(
        "guide", "guide_sections", schemas.GuideIn, schemas.GuideOut, "position ASC, id ASC",
        ["position"], _prepare_guide,
    ),
    Collection("pages", "pages", schemas.PageIn, schemas.PageOut, "title ASC"),
]  # fmt: skip


async def unique_slug(ctx: Ctx, table: str, wanted: str, exclude_id: int | None = None) -> str:
    base = slugify(wanted)
    slug, n = base, 2
    while await ctx.db.first(
        f"SELECT 1 FROM {table} WHERE slug = ? AND id != ?", slug, exclude_id or 0
    ):
        slug, n = f"{base}-{n}", n + 1
    return slug


async def load(ctx: Ctx, c: Collection, item_id: int) -> dict:
    row = await ctx.db.first(f"SELECT * FROM {c.table} WHERE id = ?", item_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return row


async def to_out(ctx: Ctx, rows: list[dict]) -> list[dict]:
    return await attach_covers(ctx.db, rows, ctx.settings.media_public_base_url)


async def _values(ctx: Ctx, c: Collection, data: schemas.ContentIn, existing: dict | None) -> dict:
    values = data.model_dump()
    if values.get("cover_image_id") is not None and not await ctx.db.first(
        "SELECT 1 FROM media WHERE id = ?", values["cover_image_id"]
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Cover image not found")
    values["body"] = clean_html(values["body"])
    values["body_bo"] = clean_html(values["body_bo"])
    for k in DATETIME_FIELDS & values.keys():
        values[k] = to_db_dt(values[k])
    values["slug"] = await unique_slug(
        ctx, c.table, data.slug or data.title, existing["id"] if existing else None
    )
    if c.prepare:
        await c.prepare(ctx, values, existing)
    return {k: values.get(k) for k in c.fields}


def build_router(c: Collection) -> APIRouter:
    router = APIRouter(prefix=f"/admin/{c.path}", tags=[f"admin:{c.path}"])
    In, Out = c.in_schema, c.out_schema

    @router.get("", response_model=schemas.Paginated[Out])
    async def list_items(
        q: str = "",
        status_: schemas.Status | None = Query(None, alias="status"),
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
        ctx: Ctx = Depends(get_ctx),
        _=Depends(get_current_user),
    ):
        where, params = ["1 = 1"], []
        if q:
            where.append("(title LIKE ? OR title_bo LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if status_:
            where.append("status = ?")
            params.append(status_)
        w = " AND ".join(where)
        total = await ctx.db.first(f"SELECT COUNT(*) AS n FROM {c.table} WHERE {w}", *params)
        rows = await ctx.db.all(
            f"SELECT * FROM {c.table} WHERE {w} ORDER BY {c.order_by} LIMIT ? OFFSET ?",
            *params, limit, (page - 1) * limit,
        )  # fmt: skip
        return {"items": await to_out(ctx, rows), "total": total["n"]}

    @router.get("/{item_id}", response_model=Out)
    async def get_item(item_id: int, ctx: Ctx = Depends(get_ctx), _=Depends(get_current_user)):
        return (await to_out(ctx, [await load(ctx, c, item_id)]))[0]

    @router.post("", response_model=Out, status_code=status.HTTP_201_CREATED)
    async def create_item(
        data: In,  # type: ignore[valid-type]
        ctx: Ctx = Depends(get_ctx),
        _=Depends(get_current_user),
    ):
        values = await _values(ctx, c, data, None)
        cols = ", ".join(values)
        marks = ", ".join("?" * len(values))
        row = await ctx.db.first(
            f"INSERT INTO {c.table} ({cols}) VALUES ({marks}) RETURNING *", *values.values()
        )
        if row["status"] == "published":
            await trigger_site_rebuild(ctx.settings)
        return (await to_out(ctx, [row]))[0]

    @router.put("/{item_id}", response_model=Out)
    async def update_item(
        item_id: int,
        data: In,  # type: ignore[valid-type]
        ctx: Ctx = Depends(get_ctx),
        _=Depends(get_current_user),
    ):
        existing = await load(ctx, c, item_id)
        values = await _values(ctx, c, data, existing)
        sets = ", ".join(f"{k} = ?" for k in values)
        row = await ctx.db.first(
            f"UPDATE {c.table} SET {sets}, updated_at = ? WHERE id = ? RETURNING *",
            *values.values(), now_iso(), item_id,
        )  # fmt: skip
        if existing["status"] == "published" or row["status"] == "published":
            await trigger_site_rebuild(ctx.settings)
        return (await to_out(ctx, [row]))[0]

    @router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_item(item_id: int, ctx: Ctx = Depends(get_ctx), _=Depends(get_current_user)):
        existing = await load(ctx, c, item_id)
        await ctx.db.run(f"DELETE FROM {c.table} WHERE id = ?", item_id)
        if existing["status"] == "published":
            await trigger_site_rebuild(ctx.settings)

    if c.path == "guide":

        @router.post("/reorder", status_code=status.HTTP_204_NO_CONTENT)
        async def reorder(
            data: schemas.ReorderIn, ctx: Ctx = Depends(get_ctx), _=Depends(get_current_user)
        ):
            ids = {r["id"] for r in await ctx.db.all("SELECT id FROM guide_sections")}
            if len(data.ids) != len(ids) or set(data.ids) != ids:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, "Send every section id exactly once"
                )
            await ctx.db.batch(
                [
                    ("UPDATE guide_sections SET position = ? WHERE id = ?", (pos, sid))
                    for pos, sid in enumerate(data.ids, start=1)
                ]
            )
            await trigger_site_rebuild(ctx.settings)

    return router


routers = [build_router(c) for c in COLLECTIONS]
