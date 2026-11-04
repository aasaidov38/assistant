"""Лекарства и витамины: расписание приёма, напоминания, отметка «принял», остаток упаковки.

Дозировку и схему вводит сам пользователь (как назначил врач или написано в инструкции).
Ассистент ничего не советует и не рассчитывает дозы — только напоминает и считает остаток.

Напоминание приходит в назначенное время с кнопками «✅ Принял», «⏰ Через 30 мин», «⏭ Пропустить».
Если не ответили — ещё одно через 45 минут. Когда упаковки осталось на low_days дней — предложит
добавить в список покупок.
Кнопки: x:i<intake_id>:take|snooze|skip|untake|info  ·  x:m<med_id>:shop|stop|stopok|info  ·  x:0:list
«ℹ️ Инструкция» (1.10.6) показывает краткую справку по названию лекарства — см. druginfo.py.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from . import db, druginfo, notify, scope, shopping
from .config import get_settings
from .schemas import Button, Reply
from .users import User
from .util import WEEKDAYS, esc, fnum, now, num, parse_date, today

REPEAT_AFTER = timedelta(minutes=45)
LATE_LIMIT = timedelta(hours=2)        # если ядро было выключено дольше — не досылаем старые напоминания


def _times(v) -> list[time]:
    out = []
    for t in (v or []):
        try:
            h, m = (str(t).replace(".", ":").split(":") + ["0"])[:2]
            out.append(time(int(h), int(m)))
        except ValueError:
            continue
    return sorted(set(out))


async def meds(family_id: int, user_id: int | None = None, active_only: bool = True) -> list[dict]:
    rows = await db.get_pool().fetch(
        "SELECT m.*, u.name AS user_name FROM meds m JOIN users u ON u.id=m.user_id WHERE m.family_id=$1 AND m.deleted_at IS NULL"
        f" AND {scope.cond('meds', 'm.user_id')}"
        + (" AND m.active" if active_only else "") + " AND ($2::int IS NULL OR m.user_id=$2) ORDER BY m.id", family_id, user_id)
    return [dict(r) for r in rows]


async def find(user: User, name: str | None) -> dict | None:
    ms = await meds(user.family_id)
    n = (name or "").lower().replace("ё", "е").strip()
    if not n:
        mine = [m for m in ms if m["user_id"] == user.id]
        return mine[0] if len(mine) == 1 else None
    for m in ms:
        if m["title"].lower().replace("ё", "е") == n:
            return m
    stems = [w[:max(3, len(w) - 2)] for w in n.split() if len(w) >= 3]
    cands = [m for m in ms if any(s in m["title"].lower().replace("ё", "е") for s in stems)]
    mine = [m for m in cands if m["user_id"] == user.id]
    return (mine or cands or [None])[0]


def per_day(m: dict) -> Decimal:
    n = len(m["times"])
    if m["days"]:
        return Decimal(m["per_intake"]) * n * len(m["days"]) / 7
    return Decimal(m["per_intake"]) * n


def days_of_stock(m: dict) -> int | None:
    if m["stock"] is None:
        return None
    pd = per_day(m)
    return int(Decimal(m["stock"]) / pd) if pd else None


def schedule_text(m: dict) -> str:
    t = ", ".join(x.strftime("%H:%M") for x in m["times"])
    days = "каждый день" if not m["days"] else ", ".join(WEEKDAYS[d][:2] for d in sorted(m["days"]))
    s = f"{t}, {days}"
    if m["end_on"]:
        s += f", до {m['end_on']:%d.%m}"
    return s


def line(m: dict) -> str:
    who = f" — для: {esc(m['for_whom'])}" if m["for_whom"] else ""
    s = f"💊 <b>{esc(m['title'])}</b>{(' ' + esc(m['dose'])) if m['dose'] else ''}{who}\n   {schedule_text(m)}"
    if m["stock"] is not None:
        d = days_of_stock(m)
        s += f" · осталось {fnum(m['stock'])} {esc(m['unit'])}" + (f" (≈ {d} дн.)" if d is not None else "")
    return s


async def add(user: User, data: dict) -> Reply:
    title = str(data.get("title") or "").strip()[:80]
    times = _times(data.get("times"))
    if not title:
        return Reply(text="Как называется лекарство? Пример: <code>витамин D по 1 таблетке в 9:00, в упаковке 60</code>")
    if not times:
        return Reply(text=f"Во сколько принимать «{esc(title)}»? Пример: <code>{esc(title)} в 9:00 и 21:00</code>")
    days = [int(d) for d in (data.get("days") or []) if str(d).isdigit() and 0 <= int(d) <= 6] or None
    start = parse_date(data.get("start_on")) or today()
    end = parse_date(data.get("end_on"))
    if not end and num(data.get("course_days")):
        end = start + timedelta(days=int(num(data.get("course_days"))) - 1)
    stock, per = num(data.get("stock")), num(data.get("per_intake")) or 1
    mid = await db.get_pool().fetchval(
        "INSERT INTO meds (family_id, user_id, for_whom, title, dose, times, days, start_on, end_on, stock, per_intake, unit)"
        " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING id",
        user.family_id, user.id, (str(data.get("for_whom") or "").strip()[:40] or None), title,
        (str(data.get("dose") or "").strip()[:60] or None), times, days, start, end, stock, per,
        (str(data.get("unit") or "шт").strip()[:10]))
    m = next(x for x in await meds(user.family_id) if x["id"] == mid)
    return Reply(text="💊 Добавил в расписание\n" + line(m) + "\n\nНапомню в это время. Отметить вручную: "
                      f"<code>принял {esc(title.lower())}</code>. Все лекарства: /meds",
                 buttons=[[Button(text="⏹ Остановить курс", action=f"x:m{mid}:stop")]])


async def _intake_text(m: dict, due: datetime, repeat: bool = False) -> Reply:
    z = get_settings().zone
    who = f" ({esc(m['for_whom'])})" if m["for_whom"] else ""
    head = "🔔 <b>Напоминаю ещё раз</b>" if repeat else "💊 <b>Время принять</b>"
    s = f"{head}\n{esc(m['title'])}{(' — ' + esc(m['dose'])) if m['dose'] else ''}{who} · {due.astimezone(z):%H:%M}"
    return Reply(text=s)


def _intake_buttons(iid: int) -> list[list[Button]]:
    return [[Button(text="✅ Принял", action=f"x:i{iid}:take")],
            [Button(text="⏰ Через 30 мин", action=f"x:i{iid}:snooze"), Button(text="⏭ Пропустить", action=f"x:i{iid}:skip")],
            [Button(text="ℹ️ Инструкция", action=f"x:i{iid}:info")]]


async def send_due() -> None:
    """Каждые 30 секунд: создаёт приёмы на наступившее время и шлёт напоминания."""
    n = now()
    z = get_settings().zone
    pool = db.get_pool()
    d = n.date()
    for r in await pool.fetch("SELECT * FROM meds WHERE active AND deleted_at IS NULL AND start_on <= $1"
                              " AND (end_on IS NULL OR end_on >= $1)", d):
        m = dict(r)
        if m["days"] and d.weekday() not in m["days"]:
            continue
        for t in m["times"]:
            due = datetime.combine(d, t, tzinfo=z)
            if not (due <= n < due + LATE_LIMIT):
                continue
            iid = await pool.fetchval("INSERT INTO med_intakes (med_id, due_at, user_id, reminded, last_sent_at)"
                                      " VALUES ($1,$2,$3,1,now()) ON CONFLICT (med_id, due_at) DO NOTHING RETURNING id",
                                      m["id"], due, m["user_id"])
            if iid:
                reply = await _intake_text(m, due)
                reply.buttons = _intake_buttons(iid)
                await notify.send(m["user_id"], reply, topic="meds", urgent=True)
    # Повтор: не ответили 45 минут, или прошло время «отложить»
    rows = await pool.fetch(
        "SELECT i.*, m.title, m.dose, m.for_whom, m.user_id AS owner FROM med_intakes i JOIN meds m ON m.id=i.med_id"
        " WHERE i.status='pending' AND i.due_at > now() - interval '12 hours'"
        " AND ((i.snooze_until IS NOT NULL AND i.snooze_until <= now())"
        "   OR (i.snooze_until IS NULL AND i.reminded < 2 AND i.last_sent_at <= now() - $1::interval))", REPEAT_AFTER)
    for i in rows:
        ok = await pool.fetchval("UPDATE med_intakes SET reminded=reminded+1, last_sent_at=now(), snooze_until=NULL"
                                 " WHERE id=$1 AND status='pending' RETURNING id", i["id"])
        if ok:
            reply = await _intake_text(dict(i), i["due_at"], repeat=True)
            reply.buttons = _intake_buttons(i["id"])
            await notify.send(i["owner"], reply, topic="meds", urgent=True)


async def _change_stock(conn, med_id: int, delta: Decimal) -> None:
    await conn.execute("UPDATE meds SET stock = greatest(stock + $2, 0) WHERE id=$1 AND stock IS NOT NULL", med_id, delta)


async def _low_stock_note(user: User, med_id: int) -> tuple[str, list[list[Button]]]:
    m = next((x for x in await meds(user.family_id, active_only=False) if x["id"] == med_id), None)
    if not m or m["stock"] is None:
        return "", []
    left = days_of_stock(m)
    if left is not None and left <= m["low_days"] and (m["low_notified_on"] is None or m["low_notified_on"] < today() - timedelta(days=3)):
        await db.get_pool().execute("UPDATE meds SET low_notified_on=$2 WHERE id=$1", med_id, today())
        return (f"\n\n📦 {esc(m['title'])} заканчивается: осталось {fnum(m['stock'])} {esc(m['unit'])} (≈ {left} дн.)",
                [[Button(text="🛒 В список покупок", action=f"x:m{med_id}:shop")]])
    return f"\nОсталось: {fnum(m['stock'])} {esc(m['unit'])}", []


async def take(user: User, intake_id: int) -> Reply:
    pool = db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        r = await conn.fetchrow("UPDATE med_intakes i SET status='taken', taken_at=now(), user_id=$2 FROM meds m"
                                " WHERE i.id=$1 AND m.id=i.med_id AND m.family_id=$3 AND i.status <> 'taken'"
                                " RETURNING i.med_id, m.title, m.per_intake", intake_id, user.id, user.family_id)
        if not r:
            return Reply(text="Уже отмечено.")
        await _change_stock(conn, r["med_id"], -Decimal(r["per_intake"]))
        await db.emit(conn, "med.taken", {"med_id": r["med_id"], "title": r["title"], "by": user.name})
    note, buttons = await _low_stock_note(user, r["med_id"])
    return Reply(text=f"✅ Принято: {esc(r['title'])} · {now():%H:%M}" + note,
                 buttons=buttons + [[Button(text="↩️ Не принимал", action=f"x:i{intake_id}:untake")]])


async def take_by_name(user: User, name: str | None) -> Reply:
    m = await find(user, name)
    if not m:
        ms = await meds(user.family_id, user.id)
        if not ms:
            return Reply(text="Лекарств в расписании нет. Добавьте: <code>витамин D по 1 таблетке в 9:00</code>")
        return Reply(text="Какое лекарство? В расписании: " + ", ".join(esc(x["title"]) for x in ms))
    pool = db.get_pool()
    # ближайший неотмеченный приём за последние 12 часов, иначе — внеплановый сейчас
    iid = await pool.fetchval("SELECT id FROM med_intakes WHERE med_id=$1 AND status='pending' AND due_at > now() - interval '12 hours'"
                              " ORDER BY due_at DESC LIMIT 1", m["id"])
    if not iid:
        iid = await pool.fetchval("INSERT INTO med_intakes (med_id, due_at, user_id) VALUES ($1, date_trunc('minute', now()), $2)"
                                  " ON CONFLICT (med_id, due_at) DO UPDATE SET user_id=EXCLUDED.user_id RETURNING id",
                                  m["id"], user.id)
    return await take(user, iid)


async def add_stock(user: User, name: str | None, amount) -> Reply:
    m = await find(user, name)
    a = num(amount)
    if not m or not a:
        return Reply(text="Пример: <code>купил витамин D, 60 таблеток</code>")
    await db.get_pool().execute("UPDATE meds SET stock=coalesce(stock,0)+$2, low_notified_on=NULL WHERE id=$1", m["id"], a)
    m = next(x for x in await meds(user.family_id) if x["id"] == m["id"])
    return Reply(text=f"📦 Пополнил: {esc(m['title'])} +{fnum(a)} {esc(m['unit'])}\n" + line(m))


async def stop_by_name(user: User, name: str | None) -> Reply:
    m = await find(user, name)
    if not m:
        return Reply(text="Не нашёл такое лекарство в расписании. Список: /meds")
    await db.get_pool().execute("UPDATE meds SET active=false, end_on=coalesce(end_on, current_date) WHERE id=$1", m["id"])
    return Reply(text=f"⏹ Курс остановлен: {esc(m['title'])}. Напоминаний больше не будет.")


async def today_status(family_id: int, user_id: int | None = None) -> list[dict]:
    """Приёмы на сегодня: что принято, что пропущено, что ещё впереди."""
    z = get_settings().zone
    d = today()
    out = []
    for m in await meds(family_id, user_id):
        if m["start_on"] > d or (m["end_on"] and m["end_on"] < d) or (m["days"] and d.weekday() not in m["days"]):
            continue
        rows = {r["due_at"]: r for r in await db.get_pool().fetch(
            "SELECT * FROM med_intakes WHERE med_id=$1 AND due_at >= $2 AND due_at < $3", m["id"],
            datetime.combine(d, time.min, tzinfo=z), datetime.combine(d + timedelta(days=1), time.min, tzinfo=z))}
        for t in m["times"]:
            due = datetime.combine(d, t, tzinfo=z)
            r = next((v for k, v in rows.items() if k == due), None)
            out.append({"med_id": m["id"], "title": m["title"], "dose": m["dose"], "for_whom": m["for_whom"],
                        "user_name": m["user_name"], "time": t.strftime("%H:%M"), "intake_id": r["id"] if r else None,
                        "status": r["status"] if r else ("upcoming" if due > now() else "missed")})
    return sorted(out, key=lambda x: x["time"])


async def list_reply(user: User, head: str = "") -> Reply:
    ms = await meds(user.family_id)
    if not ms:
        return Reply(text=(head + "\n\n" if head else "") + "💊 Лекарств в расписании нет.\n"
                     "Добавьте: <code>витамин D по 1 таблетке в 9:00, в упаковке 60</code>, "
                     "<code>младшей сироп 5 мл в 8 и 20 часов 7 дней</code>")
    st = await today_status(user.family_id)
    icon = {"taken": "✅", "skipped": "⏭", "pending": "⏳", "upcoming": "🕒", "missed": "❔"}
    text = (head + "\n\n" if head else "") + "💊 <b>Лекарства</b>\n" + "\n".join(line(m) for m in ms)
    if st:
        text += "\n\n<b>Сегодня</b>\n" + "\n".join(
            f"{icon[s['status']]} {s['time']} {esc(s['title'])}" + (f" ({esc(s['for_whom'])})" if s["for_whom"] else "") for s in st)
    text += "\n\nℹ️ Краткая инструкция к лекарству: <code>/drug название</code>"
    buttons = [[Button(text=f"✅ {s['time']} {s['title'][:20]}", action=f"x:i{s['intake_id']}:take")]
               for s in st if s["status"] == "pending" and s["intake_id"]][:4]
    return Reply(text=text, buttons=buttons)


async def handle_action(user: User, parts: list[str]) -> Reply:
    ref, op = parts[1], parts[2]
    if op == "list":
        return await list_reply(user)
    pool = db.get_pool()
    if ref.startswith("i"):
        iid = int(ref[1:])
        own = await pool.fetchrow("SELECT i.*, m.title, m.per_intake, m.family_id FROM med_intakes i JOIN meds m ON m.id=i.med_id"
                                  " WHERE i.id=$1", iid)
        if not own or own["family_id"] != user.family_id:
            return Reply(text="Напоминание устарело.")
        if op == "take":
            return await take(user, iid)
        if op == "info":
            r = await druginfo.lookup(user, own["title"])
            r.new_message = True
            return r
        if op == "snooze":
            await pool.execute("UPDATE med_intakes SET snooze_until=now() + interval '30 minutes' WHERE id=$1 AND status='pending'", iid)
            return Reply(text=f"⏰ Напомню про {esc(own['title'])} через 30 минут.")
        if op == "skip":
            await pool.execute("UPDATE med_intakes SET status='skipped' WHERE id=$1 AND status='pending'", iid)
            return Reply(text=f"⏭ Пропущено: {esc(own['title'])}.")
        if op == "untake":
            async with pool.acquire() as conn, conn.transaction():
                ok = await conn.fetchval("UPDATE med_intakes SET status='pending', taken_at=NULL, reminded=2 WHERE id=$1"
                                         " AND status='taken' RETURNING id", iid)
                if ok:
                    await _change_stock(conn, own["med_id"], Decimal(own["per_intake"]))
            return Reply(text=f"↩️ Отметка снята: {esc(own['title'])} не принят.", buttons=_intake_buttons(iid))
    if ref.startswith("m"):
        mid = int(ref[1:])
        m = next((x for x in await meds(user.family_id, active_only=False) if x["id"] == mid), None)
        if not m:
            return Reply(text="Лекарство не найдено.")
        if op == "info":
            r = await druginfo.lookup(user, m["title"])
            r.new_message = True
            return r
        if op == "shop":
            r = await shopping.add(user, [{"title": m["title"], "dept": "аптека"}])
            r.new_message = True
            return r
        if op == "stop":
            return Reply(text=f"Остановить курс «{esc(m['title'])}»? Напоминаний больше не будет.",
                         buttons=[[Button(text="⏹ Да, остановить", action=f"x:m{mid}:stopok"),
                                   Button(text="Отмена", action="x:0:list")]])
        if op == "stopok":
            return await stop_by_name(user, m["title"])
    return Reply(text="Неизвестное действие.")


async def morning_lines(user: User) -> list[str]:
    st = [s for s in await today_status(user.family_id, user.id) if s["status"] in ("upcoming", "pending")]
    if not st:
        return []
    return ["💊 Сегодня: " + ", ".join(f"{s['time']} {esc(s['title'])}" for s in st[:5])]
