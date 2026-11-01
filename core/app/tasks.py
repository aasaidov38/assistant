"""Дела и напоминания. Если TickTick подключён, задача дублируется туда и напоминает уже TickTick."""
import logging
from datetime import date, datetime, time, timedelta

from . import db, notify, ticktick, users
from .config import get_settings
from .schemas import Button, Reply
from .users import User
from .util import esc, now

log = logging.getLogger("tasks")


def _parse_due(v: str | None) -> tuple[datetime | None, bool]:
    if not v:
        return None, False
    z = get_settings().zone
    v = str(v).strip()
    try:
        if len(v) <= 10:
            return datetime.combine(date.fromisoformat(v), time.min, tzinfo=z), True
        dt = datetime.fromisoformat(v)
        return (dt if dt.tzinfo else dt.replace(tzinfo=z)), False
    except ValueError:
        return None, False


def _fmt_due(t: dict) -> str:
    if not t.get("due_at"):
        return "без срока"
    d = t["due_at"].astimezone(get_settings().zone)
    day = d.date()
    td = now().date()
    word = "сегодня" if day == td else "завтра" if day == td + timedelta(days=1) else f"{d:%d.%m.%Y}"
    return word if t.get("all_day") else f"{word} в {d:%H:%M}"


async def find_member(user: User, name: str | None) -> User | None:
    """Член семьи по имени из сообщения («Яне», «яна», «Яночка» → Яна)."""
    if not name:
        return None
    n = name.strip().lower().replace("ё", "е")
    members = await users.family_members(user.family_id)
    for m in members:
        if m.name.lower().replace("ё", "е") == n:
            return m
    for m in members:   # по началу имени: «Ян» из «Яне»
        mn = m.name.lower().replace("ё", "е")
        k = max(2, min(len(mn), len(n), 5) - 1)   # без окончания: «Яне» и «Яна» → «Ян»
        if len(n) >= 2 and mn[:k] == n[:k]:
            return m
    return None


async def save(user: User, data: dict, source: str) -> tuple[dict, str]:
    title = (data.get("title") or "").strip()[:300] or "Без названия"
    due_at, all_day = _parse_due(data.get("due"))
    remind_min = data.get("remind_minutes_before")
    assignee = await find_member(user, data.get("assignee"))
    if assignee and assignee.id == user.id:
        assignee = None
    owner_id = assignee.id if assignee else user.id     # чьё это дело: в чей TickTick и кому напоминать

    # 1) TickTick (если подключён у исполнителя)
    tt, note = None, ""
    try:
        tt = await ticktick.create_task(owner_id, title, data.get("notes"), due_at, all_day,
                                        remind_min if remind_min is not None else 0)
        if tt:
            note = "📲 Добавлено в TickTick"
    except Exception as e:
        log.warning("TickTick: %s", e)
        note = "⚠️ В TickTick добавить не удалось, напомню сам"

    # 2) Своё напоминание, только если задача не ушла в TickTick
    remind_at = None
    if due_at and not tt:
        if all_day:
            remind_at = due_at.replace(hour=9)
        else:
            remind_at = due_at - timedelta(minutes=int(remind_min or 0))
        if remind_at < now():
            remind_at = None

    async with db.get_pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "INSERT INTO tasks (family_id, user_id, title, notes, due_at, all_day, remind_at,"
            " ticktick_id, ticktick_project_id, assignee_id, ticktick_list, source)"
            " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING *",
            user.family_id, user.id, title, data.get("notes"), due_at, all_day, remind_at,
            tt.get("id") if tt else None, tt.get("projectId") if tt else None, assignee.id if assignee else None,
            "Входящие" if tt else None, source if source in ("web", "alice", "voice") else "bot")
        task = dict(row)
        task["assignee_name"] = assignee.name if assignee else None
        await db.emit(conn, "task.created", task)
    if remind_at:
        who = f" ({assignee.name})" if assignee else ""
        note = (note + "\n" if note else "") + \
            f"⏰ Напомню{esc(who)}: {remind_at.astimezone(get_settings().zone):%d.%m в %H:%M}"
    if assignee:
        sent = await notify.send(assignee.id, Reply(
            text=f"📝 {esc(user.name)} поставил(а) вам задачу: <b>{esc(title)}</b>\n📅 {_fmt_due(task)}"
                 + (f"\n{esc(data.get('notes'))}" if data.get("notes") else ""),
            buttons=[[Button(text="✅ Выполнено", action=f"k:{task['id']}:done")]]))
        note = (note + "\n" if note else "") + (f"📨 {esc(assignee.name)} получит уведомление" if sent
                                                 else f"⚠️ Не смог отправить уведомление: {esc(assignee.name)}")
    return task, note


def render_saved(task: dict, note: str = "", header: str = "") -> Reply:
    who = f"\n👤 Для: {esc(task['assignee_name'])}" if task.get("assignee_name") else ""
    text = (header + "\n" if header else "") + f"📝 Задача: <b>{esc(task['title'])}</b>\n📅 {_fmt_due(task)}{who}"
    if note:
        text += "\n" + note
    tid = task["id"]
    return Reply(text=text, buttons=[[Button(text="✅ Выполнено", action=f"k:{tid}:done"),
                                      Button(text="❌ Удалить", action=f"k:{tid}:del")]])


async def handle_action(user: User, parts: list[str]) -> Reply:
    """k:<id>:done | k:<id>:del | k:0:sync"""
    if parts[2] == "sync":
        return await sync_now(user)
    if parts[2] == "ttn":          # k:0:ttn:1 / k:0:ttn:0 — уведомления о задачах из TickTick
        return await set_tt_notify(user, parts[3] == "1")
    task_id = int(parts[1])
    pool = db.get_pool()
    task = await pool.fetchrow("SELECT * FROM tasks WHERE id=$1 AND family_id=$2", task_id, user.family_id)
    if not task or task["status"] != "open":
        return Reply(text="Задача не найдена или уже закрыта.")
    status = "done" if parts[2] == "done" else "deleted"
    owner_id = task["assignee_id"] or task["user_id"]
    tt_note = ""
    try:
        if task["ticktick_id"]:
            if status == "done":
                await ticktick.complete_task(owner_id, task["ticktick_project_id"], task["ticktick_id"])
            else:
                await ticktick.delete_task(owner_id, task["ticktick_project_id"], task["ticktick_id"])
            tt_note = "\n📲 И в TickTick"
    except Exception as e:
        log.warning("TickTick: %s", e)
        tt_note = "\n⚠️ В TickTick не дошло, повторю при следующей синхронизации"
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("UPDATE tasks SET status=$2, done_at=now(), updated_at=now() WHERE id=$1", task_id, status)
        await db.emit(conn, f"task.{status}", {"id": task_id})
    # Автору сообщим, что назначенное им дело выполнено
    if status == "done" and task["user_id"] != user.id:
        await notify.send(task["user_id"], Reply(text=f"✅ {esc(user.name)} выполнил(а): {esc(task['title'])}"), topic="tasks")
    return Reply(text=("✅ Выполнено: " if status == "done" else "🗑 Удалено: ") + esc(task["title"]) + tt_note)


async def list_open(user: User) -> Reply:
    rows = await db.get_pool().fetch(
        "SELECT t.*, coalesce(a.name, u.name) AS user_name, coalesce(t.assignee_id, t.user_id) AS owner_id"
        " FROM tasks t JOIN users u ON u.id=t.user_id LEFT JOIN users a ON a.id=t.assignee_id"
        " WHERE t.family_id=$1 AND t.status='open'"
        " ORDER BY t.due_at NULLS LAST, t.id LIMIT 20", user.family_id)
    if not rows:
        sync = [[Button(text="🔄 Синхронизировать с TickTick", action="k:0:sync")]] if await ticktick.is_connected(user.id) else []
        return Reply(text="✅ Открытых задач нет.\nНапишите, например: <code>завтра в 10 позвонить в банк</code>",
                     buttons=sync)
    text = "📋 <b>Открытые задачи</b>\n"
    buttons = []
    for i, r in enumerate(rows, 1):
        who = "" if r["owner_id"] == user.id else f" · 👤 {esc(r['user_name'])}"
        tt = " 📲" if r["ticktick_id"] else ""
        text += f"{i}. {esc(r['title'])} · {_fmt_due(dict(r))}{who}{tt}\n"
        buttons.append([Button(text=f"✅ {i}. {r['title'][:28]}", action=f"k:{r['id']}:done")])
    if await ticktick.is_connected(user.id):
        text += "\n📲 — задача есть в TickTick"
        buttons.append([Button(text="🔄 Синхронизировать с TickTick", action="k:0:sync")])
    return Reply(text=text, buttons=buttons)


async def sync_now(user: User) -> Reply:
    import time as _t
    from . import ticktick_sync     # здесь, чтобы не было циклического импорта
    if not await ticktick.is_connected(user.id):
        return Reply(text="TickTick не подключён: /ticktick")
    try:
        st = await ticktick_sync.sync_user(user.id, user.family_id, notify_new=False)
        await ticktick.save_sync_state(user.id, last_sync=int(_t.time()), last_error=None)
    except Exception as e:
        return Reply(text=f"⚠️ TickTick: {esc(str(e))[:300]}")
    reply = await list_open(user)
    reply.text = (f"🔄 Синхронизировано: новых {st['new']}, изменено {st['updated']}, закрыто {st['closed']}"
                  f"{', дослано ' + str(st['pushed']) if st['pushed'] else ''}\n\n" + reply.text)
    return reply


async def due_reminders() -> list[dict]:
    """Задачи, по которым пора напомнить. Помечает их как отправленные."""
    async with db.get_pool().acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            "UPDATE tasks SET reminded_at=now() WHERE id IN ("
            " SELECT id FROM tasks WHERE status='open' AND reminded_at IS NULL AND remind_at <= now()"
            " FOR UPDATE SKIP LOCKED LIMIT 50) RETURNING *, coalesce(assignee_id, user_id) AS owner_id")
    return [dict(r) for r in rows]


async def overdue(family_id: int) -> list[dict]:
    rows = await db.get_pool().fetch(
        "SELECT t.title, t.due_at, coalesce(a.name, u.name) AS owner FROM tasks t JOIN users u ON u.id=t.user_id"
        " LEFT JOIN users a ON a.id=t.assignee_id WHERE t.family_id=$1 AND t.status='open' AND t.due_at < now()"
        " ORDER BY t.due_at LIMIT 20", family_id)
    return [dict(r) for r in rows]


async def set_tt_notify(user: User, on: bool) -> Reply:
    if not (await ticktick.token_info(user.id)).get("expires_at"):
        return Reply(text="TickTick не подключён: /ticktick")
    await ticktick.save_sync_state(user.id, notify=on)
    if on:
        return Reply(text="🔔 Буду сообщать о новых задачах из TickTick.\nВыключить: <code>/ticktick off</code>",
                     buttons=[[Button(text="🔕 Не присылать", action="k:0:ttn:0")]])
    return Reply(text="🔕 Сообщения о новых задачах из TickTick выключены. Синхронизация продолжает работать: "
                      "задачи видны в /tasks, панели и утренней сводке.\nВключить: <code>/ticktick on</code>",
                 buttons=[[Button(text="🔔 Присылать", action="k:0:ttn:1")]])
