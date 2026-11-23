"""Tiny async database layer: Cloudflare D1 in the Worker, sqlite3 in tests and scripts.

All SQL is plain SQLite (D1 *is* SQLite). Rows come back as plain Python dicts.
"""

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

DT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def now_iso() -> str:
    return datetime.now(UTC).strftime(DT_FORMAT)


def to_db_dt(dt: datetime | None) -> str | None:
    """Store every datetime as UTC text so SQL string comparison sorts correctly."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime(DT_FORMAT)


def _param(v: Any) -> Any:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, datetime):
        return to_db_dt(v)
    return v


class Database(Protocol):
    async def all(self, sql: str, *params: Any) -> list[dict]: ...
    async def first(self, sql: str, *params: Any) -> dict | None: ...
    async def run(self, sql: str, *params: Any) -> int: ...
    async def batch(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> None: ...


# ---------- Cloudflare D1 (inside the Worker) ----------


def _get(obj: Any, name: str) -> Any:
    try:
        return obj[name]
    except (TypeError, KeyError, AttributeError):
        return getattr(obj, name)


def _to_dict(row: Any) -> dict:
    if row is None:
        return None
    if hasattr(row, "to_py"):
        row = row.to_py()
    return dict(row)


class D1Database:
    def __init__(self, binding: Any):
        self.binding = binding

    def _stmt(self, sql: str, params: Sequence[Any]):
        stmt = self.binding.prepare(sql)
        return stmt.bind(*[_param(p) for p in params]) if params else stmt

    async def all(self, sql: str, *params: Any) -> list[dict]:
        res = await self._stmt(sql, params).all()
        return [_to_dict(r) for r in _get(res, "results")]

    async def first(self, sql: str, *params: Any) -> dict | None:
        row = await self._stmt(sql, params).first()
        return _to_dict(row) if row is not None else None

    async def run(self, sql: str, *params: Any) -> int:
        res = await self._stmt(sql, params).run()
        return int(_get(_get(res, "meta"), "changes") or 0)

    async def batch(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> None:
        if statements:
            await self.binding.batch([self._stmt(sql, p) for sql, p in statements])


# ---------- sqlite3 (tests, local scripts) ----------


class SqliteDatabase:
    def __init__(self, path: str = ":memory:", migrate: bool = True):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")  # D1 enforces foreign keys too
        if migrate:
            for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
                self.conn.executescript(f.read_text())

    async def all(self, sql: str, *params: Any) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, [_param(p) for p in params])]

    async def first(self, sql: str, *params: Any) -> dict | None:
        row = self.conn.execute(sql, [_param(p) for p in params]).fetchone()
        self.conn.commit()  # for INSERT ... RETURNING
        return dict(row) if row else None

    async def run(self, sql: str, *params: Any) -> int:
        cur = self.conn.execute(sql, [_param(p) for p in params])
        self.conn.commit()
        return cur.rowcount

    async def batch(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> None:
        with self.conn:  # one transaction, like D1 batch
            for sql, params in statements:
                self.conn.execute(sql, [_param(p) for p in params])
