"""Синхронизация TickTick → ассистент (обратное направление).

Раз в 5 минут для каждого, у кого подключён TickTick:
  • новые задачи из TickTick появляются в боте, веб-панели и утренней сводке;
  • изменённые в TickTick название, заметка и срок обновляются у нас;
  • выполненные или удалённые в TickTick закрываются у нас;
  • если задачу закрыли у нас, а в TickTick она ещё открыта (не дошло), повторяем отправку.
Напоминания по таким задачам делает сам TickTick, бот не дублирует.
"""
import logging
import time
from datetime import date, datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

from . import db, notify, ticktick
from .config import get_settings
from .schemas import Button, Reply
from .util import esc

log = logging.getLogger("ticktick.sync")
INTERVAL = 300
_last_run = 0.0


def parse_due(t: dict) -> tuple[datetime | None, bool]:
    raw = t.get("dueDate") or t.get("startDate")
    if not raw:
        return None, False
    try:
        dt = datetime.strptime(raw[:19] + raw[-5:], "%Y-%m-%dT%H:%M:%S%z") if raw[-5] in "+-" \
            else datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, False
    z = get_settings().zone
    if t.get("isAllDay"):
        try:
            tz = ZoneInfo(t.get("timeZone") or get_settings().tz)
        except Exception:
            tz = z
        d: date = dt.astimezone(tz).date()
        return datetime.combine(d, dtime.min, tzinfo=z), True
    return dt.astimezone(timezone.utc), False


async def sync_user(user_id: int, family_id: int, notify_new: bool = True) -> dict:
    """Возвращает статистику: {"new": n, "updated": n, "closed": n, "pushed": n}."""
    stat = {"new": 0, "updated": 0, "closed": 0, "pushed": 0}
    lists = {p["id"]: p.get("name") or "" for p in await ticktick.projects(user_id) if not p.get("closed")}
    pool = db.get_pool()
    # «Входящие» в списке проектов не приходят: их id узнаём по уже созданным задачам
    inbox = await pool.fetchval(
        "SELECT ticktick_project_id FROM tasks WHERE ticktick_project_id LIKE 'inbox%' LIMIT 1") or "inbox"
    lists.setdefault(inbox, "Входящие")
    remote: dict[str, dict] = {}
    for pid, name in lists.items():
        items = await ticktick.project_tasks(user_id, pid)
        for t in items or []:
            if t.get("status", 0) == 0:
                remote[t["id"]] = {**t, "_list": name or "Входящие"}

    local = {r["ticktick_id"]: dict(r) for r in await pool.fetch(
        "SELECT id, title, notes, due_at, all_day, status, ticktick_id, ticktick_project_id FROM tasks"
        " WHERE ticktick_id IS NOT NULL AND coalesce(assignee_id, user_id)=$1", user_id)}

    new_titles = []
    for tid, t in remote.items():
        due, all_day = parse_due(t)
        title = (t.get("title") or "Без названия")[:300]
        notes = t.get("content") or t.get("desc") or None
        l = local.get(tid)
        if l is None:
            async with pool.acquire() as conn, conn.transaction():
                row = await conn.fetchrow(
                    "INSERT INTO tasks (family_id, user_id, title, notes, due_at, all_day, ticktick_id, ticktick_project_id,"
                    " ticktick_list, source, updated_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'ticktick',now())"
                    " ON CONFLICT (ticktick_id) WHERE ticktick_id IS NOT NULL DO NOTHING RETURNING *",
                    family_id, user_id, title, notes, due, all_day, tid, t.get("projectId"), t["_list"])
                if row:
                    await db.emit(conn, "task.created", {**dict(row), "via": "ticktick"})
                    stat["new"] += 1
                    new_titles.append((row["id"], title, due, all_day))
        elif l["status"] == "open":
            if (l["title"], l["notes"], l["due_at"], l["all_day"]) != (title, notes, due, all_day):
                await pool.execute(
                    "UPDATE tasks SET title=$2, notes=$3, due_at=$4, all_day=$5, ticktick_project_id=$6, ticktick_list=$7,"
                    " remind_at=NULL, updated_at=now() WHERE id=$1",
                    l["id"], title, notes, due, all_day, t.get("projectId"), t["_list"])
                stat["updated"] += 1
        else:
            # У нас закрыта, в TickTick открыта: повторяем отправку
            try:
                if l["status"] == "done":
                    await ticktick.complete_task(user_id, l["ticktick_project_id"], tid)
                else:
                    await ticktick.delete_task(user_id, l["ticktick_project_id"], tid)
                stat["pushed"] += 1
            except ticktick.TickTickError as e:
                log.warning("Повторная отправка в TickTick: %s", e)

    for tid, l in local.items():
        if l["status"] != "open" or tid in remote:
            continue
        try:
            t = await ticktick.get_task(user_id, l["ticktick_project_id"], tid)
        except ticktick.TickTickError:
            continue          # не уверены — ничего не трогаем
        if t is not None and t.get("status", 0) == 0:
            continue          # задача жива, просто в списке, который мы не видим
        status = "deleted" if t is None else "done"
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("UPDATE tasks SET status=$2, done_at=now(), updated_at=now() WHERE id=$1", l["id"], status)
            await db.emit(conn, f"task.{status}", {"id": l["id"], "via": "ticktick"})
        stat["closed"] += 1

    if new_titles and notify_new:
        z = get_settings().zone
        lines = []
        for _id, title, due, all_day in new_titles[:15]:
            when = "" if not due else f" · {due.astimezone(z):%d.%m}" + ("" if all_day else f" {due.astimezone(z):%H:%M}")
            lines.append(f"• {esc(title)}{when}")
        more = f"\n…и ещё {len(new_titles) - 15}" if len(new_titles) > 15 else ""
        await notify.send(user_id, Reply(
            text="📲 <b>Из TickTick</b>\n" + "\n".join(lines) + more,
            buttons=[[Button(text=f"✅ {t[:28]}", action=f"k:{i}:done")] for i, t, *_ in new_titles[:5]]))
    return stat


async def run(force: bool = False) -> dict[int, dict]:
    """Вызывается фоновой задачей; по умолчанию не чаще раза в 5 минут."""
    global _last_run
    if not ticktick.configured() or (not force and time.time() - _last_run < INTERVAL):
        return {}
    _last_run = time.time()
    rows = await db.get_pool().fetch(
        "SELECT i.user_id, u.family_id, i.data FROM integrations i JOIN users u ON u.id=i.user_id WHERE i.provider='ticktick'")
    out = {}
    for r in rows:
        if not await ticktick.is_connected(r["user_id"]):
            if r["data"].get("expires_at") and not r["data"].get("expired_notified"):
                await notify.send(r["user_id"], Reply(text="⚠️ Доступ к TickTick истёк. Задачи пока живут только в боте.\n"
                                                           "Подключите заново: /ticktick"))
                await ticktick.save_sync_state(r["user_id"], expired_notified=True)
            continue
        first = not r["data"].get("last_sync")      # первый раз импортируем молча
        try:
            quiet = first or r["data"].get("notify") is False      # уведомления выключены: /ticktick off
            out[r["user_id"]] = await sync_user(r["user_id"], r["family_id"], notify_new=not quiet)
            await ticktick.save_sync_state(r["user_id"], last_sync=int(time.time()), last_error=None)
        except Exception as e:
            log.warning("Синхронизация TickTick для %s: %s", r["user_id"], e)
            await ticktick.save_sync_state(r["user_id"], last_error=str(e)[:300])
    return out
