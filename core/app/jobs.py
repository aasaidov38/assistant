"""Фоновые задачи ядра: напоминания и отправка событий в n8n."""
import asyncio
import logging

import httpx

from . import balance, bills, debts, digest, market, release, garage, meds, meters, morning, notify, papers, tasks, ticktick_sync, trips
from .config import get_settings
from .db import get_pool
from .schemas import Button, Reply
from .util import esc

log = logging.getLogger("jobs")
_client = httpx.AsyncClient(timeout=15)


async def send_reminders() -> None:
    for t in await tasks.due_reminders():
        reply = Reply(text=f"⏰ <b>Напоминание</b>\n{esc(t['title'])}" + (f"\n{esc(t['notes'])}" if t["notes"] else ""),
                      buttons=[[Button(text="✅ Выполнено", action=f"k:{t['id']}:done")]])
        await notify.send(t["owner_id"], reply, urgent=True)


async def deliver_events() -> None:
    """Отправляет новые события в n8n. При ошибке повторяет позже (до 10 попыток)."""
    url = get_settings().n8n_webhook_url
    if not url:
        return
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT id, type, payload, created_at FROM events WHERE delivered_at IS NULL AND attempts < 10"
        " AND created_at > now() - interval '1 day' ORDER BY id LIMIT 50")
    for r in rows:
        try:
            resp = await _client.post(url, json={"id": r["id"], "type": r["type"],
                                                 "created_at": r["created_at"].isoformat(),
                                                 "payload": r["payload"]},
                                      headers={"X-API-Key": get_settings().core_api_key})
            resp.raise_for_status()
            await pool.execute("UPDATE events SET delivered_at=now(), attempts=attempts+1 WHERE id=$1", r["id"])
        except Exception as e:
            await pool.execute("UPDATE events SET attempts=attempts+1, last_error=$2 WHERE id=$1",
                               r["id"], str(e)[:500])
            break   # n8n недоступен: не долбим, попробуем в следующем цикле


async def cleanup() -> None:
    """Удаляет просроченные ссылки входа, сессии веб-панели и коды привязки."""
    await get_pool().execute("DELETE FROM web_login_tokens WHERE expires_at < now() - interval '1 day'")
    await get_pool().execute("DELETE FROM web_sessions WHERE expires_at < now()")
    await get_pool().execute("DELETE FROM link_codes WHERE expires_at < now()")


async def loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        for job in (send_reminders, meds.send_due, deliver_events, digest.send_due, morning.send_due, cleanup,
                    ticktick_sync.run, notify.flush_outbox, release.check, debts.send_due, bills.send_due, papers.send_due, meters.send_due, garage.send_due, trips.send_due, market.refresh_due, balance.refresh_due):
            try:
                await job()
            except Exception:
                log.exception("Ошибка в фоновой задаче %s", job.__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
