"""Исходящие сообщения пользователю (напоминания, уведомления из n8n).

Каждый канал добавляется своей функцией в SENDERS. Сейчас есть Telegram;
позже сюда же добавятся, например, SMS или push.
"""
import logging

import httpx

from . import users
from .config import get_settings
from .schemas import Reply

log = logging.getLogger("notify")
_client = httpx.AsyncClient(timeout=20)


async def _telegram(chat_id: str, reply: Reply) -> None:
    token = get_settings().telegram_bot_token
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
    body: dict = {"chat_id": chat_id, "text": reply.text, "parse_mode": "HTML"}
    if reply.buttons:
        body["reply_markup"] = {"inline_keyboard": [
            [{"text": b.text, "url": b.url} if b.url else {"text": b.text, "callback_data": b.action}
             for b in row] for row in reply.buttons]}
    r = await _client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=body)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram {r.status_code}: {r.text[:200]}")


SENDERS = {"telegram": _telegram}


async def send(user_id: int, reply: Reply, topic: str | None = None, urgent: bool = False,
               quiet: str = "defer") -> bool:
    """Отправляет сообщение пользователю в первый доступный канал.

    topic — тема напоминания (prefs.TOPICS): если человек её выключил в настройках, сообщение не отправляется.
    urgent — отправить даже в тихие часы (лекарства, вход в панель, напоминания с точным временем).
    quiet — что делать в тихие часы: defer — отложить до их конца, drop — не отправлять (копии действий)."""
    from . import prefs
    s = await prefs.get(user_id)
    if topic and not s["notify"].get(topic, True):
        return False
    if not urgent:
        until = prefs.quiet_until(s)
        if until:
            if quiet == "drop":
                return False
            from .db import get_pool
            await get_pool().execute("INSERT INTO outbox (user_id, reply, send_after) VALUES ($1, $2::jsonb, $3)",
                                     user_id, reply.model_dump(), until)
            return True
    return await deliver(user_id, reply)


async def deliver(user_id: int, reply: Reply) -> bool:
    """Отправка без проверок настроек."""
    for channel, ext_id in await users.identities(user_id):
        sender = SENDERS.get(channel)
        if not sender:
            continue
        try:
            await sender(ext_id, reply)
            return True
        except Exception as e:
            log.warning("Не удалось отправить в %s: %s", channel, e)
    return False


async def remind(family_id: int, author_id: int | None, reply: Reply, topic: str | None = None,
                 section: str | None = None) -> None:
    """Напоминание по записи семьи. Кому — задаёт REMINDERS_TO в .env:
    owner (по умолчанию) — владельцу, первому в ALLOWED_USERS; author — автору записи; all — всем; или имя.
    Если раздел (section) личный, напоминание всегда получает автор записи."""
    mode = (get_settings().reminders_to or "owner").strip()
    members = await users.family_members(family_id)
    if not members:
        return
    if section and author_id:
        from . import scope
        if await scope.personal(family_id, section):
            await send(author_id, reply, topic=topic)
            return
    if mode == "all":
        targets = [m.id for m in members]
    elif mode == "author" and author_id:
        targets = [author_id]
    elif mode not in ("owner", "author", "all"):
        targets = [m.id for m in members if m.name.lower() == mode.lower()] or [members[0].id]
    else:
        targets = [members[0].id]          # владелец: первый пользователь семьи
    for uid in targets:
        await send(uid, reply, topic=topic)


async def flush_outbox() -> None:
    """Отправляет сообщения, отложенные на тихие часы (вызывается фоновой задачей)."""
    from .db import get_pool
    pool = get_pool()
    rows = await pool.fetch("UPDATE outbox SET sent_at=now() WHERE id IN (SELECT id FROM outbox WHERE sent_at IS NULL"
                            " AND send_after <= now() ORDER BY id LIMIT 30) RETURNING user_id, reply")
    for r in rows:
        data = r["reply"]
        if isinstance(data, str):
            import json
            data = json.loads(data)
        try:
            await deliver(r["user_id"], Reply(**data))
        except Exception as e:
            log.warning("Отложенное сообщение не отправлено: %s", e)
    await pool.execute("DELETE FROM outbox WHERE sent_at < now() - interval '7 days'")


async def remind_about(topic: str, family_id: int, author_id: int | None, reply: Reply) -> None:
    """remind для разделов, у которых тема уведомлений совпадает с названием раздела
    (bills, papers, meters, garage, trips, debts)."""
    await remind(family_id, author_id, reply, topic=topic, section=topic)
