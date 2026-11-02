"""Телефоны семьи (версия 1.9): личный ключ устройства, по которому приложение присылает данные.

Как подключить: в приложении для Android → ⚙️ Настройки → 🔌 Интеграции → «📱 Подключить этот телефон».
Панель (уже вошедшая через бота) просит у сервера новый ключ и передаёт его приложению. В базе хранится
только хеш ключа. Отозвать доступ можно там же, в «Интеграциях», — следующая отправка с телефона получит отказ.
"""
import hashlib
import json
import secrets

from . import db, prefs, users

KEY_PREFIX = "dev_"


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def create(user: users.User, name: str, info: dict | None = None) -> tuple[int, str]:
    """Новый ключ для телефона. Сам ключ возвращается один раз и больше нигде не показывается."""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    name = (str(name or "").strip() or "Телефон")[:60]
    did = await db.get_pool().fetchval(
        "INSERT INTO devices (user_id, name, key_hash, info) VALUES ($1,$2,$3,$4::jsonb) RETURNING id",
        user.id, name, _hash(key), info or {})
    return did, key


async def by_key(key: str) -> dict | None:
    """Устройство по ключу (только действующее) + отметка «был на связи»."""
    if not key or not key.startswith(KEY_PREFIX) or len(key) > 100:
        return None
    row = await db.get_pool().fetchrow(
        "UPDATE devices SET last_seen=now() WHERE key_hash=$1 AND revoked_at IS NULL"
        " RETURNING id, user_id, name, info", _hash(key))
    return dict(row) if row else None


async def save_info(device_id: int, info: dict) -> None:
    clean = {k: v for k, v in (info or {}).items()
             if k in ("app", "android", "sdk", "model", "perms", "last", "health_at", "bank_on") and len(json.dumps(v)) < 500}
    if clean:
        await db.get_pool().execute("UPDATE devices SET info = info || $2::jsonb WHERE id=$1", device_id, clean)


async def list_family(user: users.User) -> list[dict]:
    rows = await db.get_pool().fetch(
        "SELECT d.id, d.user_id, u.name AS owner, d.name, d.info, d.created_at, d.last_seen FROM devices d"
        " JOIN users u ON u.id=d.user_id WHERE u.family_id=$1 AND d.revoked_at IS NULL ORDER BY d.id",
        user.family_id)
    out = []
    for r in rows:
        r = dict(r)
        if isinstance(r["info"], str):
            r["info"] = json.loads(r["info"] or "{}")
        r["mine"] = r["user_id"] == user.id
        r["sims"] = [dict(s) for s in await db.get_pool().fetch(
            "SELECT slot, operator, balance, error, checked_at FROM sim_cards WHERE device_id=$1 ORDER BY slot", r["id"])]
        out.append(r)
    return out


async def revoke(user: users.User, device_id: int) -> bool:
    """Отозвать может хозяин телефона или владелец ассистента."""
    members = await users.family_members(user.family_id)
    owner = bool(members) and members[0].id == user.id
    n = await db.get_pool().fetchval(
        "WITH d AS (UPDATE devices SET revoked_at=now() WHERE id=$1 AND revoked_at IS NULL"
        " AND user_id IN (SELECT id FROM users WHERE family_id=$2) AND (user_id=$3 OR $4) RETURNING 1)"
        " SELECT count(*) FROM d", device_id, user.family_id, user.id, owner)
    return bool(n)


async def config(device: dict) -> dict:
    """Что телефону собирать. Списки банков и USSD-коды живут на сервере — их можно поправить без нового APK."""
    from . import bankfeed, simcards
    s = await prefs.get(device["user_id"])
    return {"user_id": device["user_id"], "device": device["name"],
            "bank_on": bool(s.get("bank_auto")),
            "bank_packages": bankfeed.PACKAGES, "bank_labels": bankfeed.LABELS_RE, "bank_senders": bankfeed.SENDERS,
            "ussd": simcards.USSD, "sim_hours": 6, "health_days": 7}
