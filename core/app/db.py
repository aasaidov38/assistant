"""Подключение к PostgreSQL и применение миграций."""
import contextvars
import json
import logging
from pathlib import Path

import asyncpg

log = logging.getLogger("db")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

pool: asyncpg.Pool | None = None
# Демо-кабинет (1.10.7): у каждого посетителя своя база. Запрос кладёт её пул сюда, get_pool() отдаёт его вместо общего.
# В обычной установке переменная всегда пуста, поведение не меняется.
_override: contextvars.ContextVar = contextvars.ContextVar("db_pool_override", default=None)


async def _init_conn(conn: asyncpg.Connection) -> None:
    # jsonb <-> dict автоматически
    await conn.set_type_codec(
        "jsonb",
        encoder=lambda v: json.dumps(v, ensure_ascii=False, default=str),
        decoder=json.loads,
        schema="pg_catalog",
    )


async def connect(dsn: str) -> asyncpg.Pool:
    global pool
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10, init=_init_conn)
    return pool


async def close() -> None:
    if pool:
        await pool.close()


def get_pool() -> asyncpg.Pool:
    p = _override.get()
    if p is not None:
        return p
    assert pool is not None, "База не подключена"
    return pool


async def migrate() -> None:
    """Выполняет новые файлы migrations/NNN_*.sql по порядку, каждый в своей транзакции."""
    async with get_pool().acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock(424242)")
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
            for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if f.name in done:
                    continue
                log.info("Применяю миграцию %s", f.name)
                async with conn.transaction():
                    await conn.execute(f.read_text(encoding="utf-8"))
                    await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", f.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(424242)")


async def emit(conn: asyncpg.Connection, event_type: str, payload: dict) -> None:
    """Записывает событие в журнал. Отправкой в n8n занимается фоновая задача."""
    await conn.execute("INSERT INTO events (type, payload) VALUES ($1, $2)", event_type, payload)
