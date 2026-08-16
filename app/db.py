import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite

from app.settings import get_settings


@asynccontextmanager
async def open_db() -> AsyncIterator[aiosqlite.Connection]:
    settings = get_settings()
    db_path = settings.app_database_path
    db_dir = os.path.dirname(os.path.abspath(db_path))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    timeout_seconds = max(settings.sqlite_busy_timeout_ms / 1000, 1)
    conn = await aiosqlite.connect(db_path, timeout=timeout_seconds)
    conn.row_factory = aiosqlite.Row
    await conn.execute(f"PRAGMA busy_timeout = {int(settings.sqlite_busy_timeout_ms)}")
    await conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        await conn.commit()
    finally:
        await conn.close()


async def fetch_one(sql: str, params: tuple = ()) -> dict | None:
    async with open_db() as db:
        cur = await db.execute(sql, params)
        row = await cur.fetchone()
        return dict(row) if row else None


async def fetch_all(sql: str, params: tuple = ()) -> list[dict]:
    async with open_db() as db:
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()
        return [dict(row) for row in rows]


async def execute(sql: str, params: tuple = ()) -> None:
    async with open_db() as db:
        await db.execute(sql, params)
