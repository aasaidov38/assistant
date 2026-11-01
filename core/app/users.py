"""Пользователи и семья."""
from dataclasses import dataclass

from .config import get_settings
from .db import get_pool


@dataclass
class User:
    id: int
    family_id: int
    name: str
    kcal_goal: int | None


async def sync_allowed_users() -> None:
    """Создаёт семью и пользователей из ALLOWED_USERS (запускается при старте ядра)."""
    s = get_settings()
    async with get_pool().acquire() as conn, conn.transaction():
        fam = await conn.fetchval("SELECT id FROM families ORDER BY id LIMIT 1")
        if fam is None:
            fam = await conn.fetchval("INSERT INTO families (name) VALUES ($1) RETURNING id", s.family_name)
        for tid, name in s.allowed():
            uid = await conn.fetchval(
                "SELECT user_id FROM user_identities WHERE channel='telegram' AND external_id=$1", tid)
            if uid is None:
                uid = await conn.fetchval(
                    "INSERT INTO users (family_id, name) VALUES ($1, $2) RETURNING id", fam, name)
                await conn.execute(
                    "INSERT INTO user_identities (channel, external_id, user_id) VALUES ('telegram', $1, $2)",
                    tid, uid)
            else:
                await conn.execute("UPDATE users SET name=$2 WHERE id=$1", uid, name)


async def resolve(channel: str, external_id: str) -> User | None:
    row = await get_pool().fetchrow(
        "SELECT u.id, u.family_id, u.name, u.kcal_goal FROM user_identities i "
        "JOIN users u ON u.id = i.user_id WHERE i.channel=$1 AND i.external_id=$2",
        channel, external_id)
    return User(**dict(row)) if row else None


async def get(user_id: int) -> User | None:
    row = await get_pool().fetchrow("SELECT id, family_id, name, kcal_goal FROM users WHERE id=$1", user_id)
    return User(**dict(row)) if row else None


async def identities(user_id: int) -> list[tuple[str, str]]:
    rows = await get_pool().fetch(
        "SELECT channel, external_id FROM user_identities WHERE user_id=$1", user_id)
    return [(r["channel"], r["external_id"]) for r in rows]


async def family_members(family_id: int) -> list[User]:
    rows = await get_pool().fetch(
        "SELECT id, family_id, name, kcal_goal FROM users WHERE family_id=$1 ORDER BY id", family_id)
    return [User(**dict(r)) for r in rows]
