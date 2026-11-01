"""Счётчики воды, света, газа, тепла: показания (текстом или фото), расход, напоминания, поверка.

Каждый месяц в день submit_from (по умолчанию 20-е) бот просит прислать показания; если к submit_to − 1
ещё не все — напоминает ещё раз. Когда все показания за месяц собраны, присылает их одним списком,
чтобы удобно переписать в Госуслуги или приложение УК.
Кнопки: r:<reading_id>:m:<meter_id> (выбрать счётчик для показания с фото) | r:0:list
"""
from datetime import date, timedelta
from decimal import Decimal

from . import db, notify, scope
from .config import get_settings
from .schemas import Button, Reply
from .users import User
from .util import esc, fnum, now, num, parse_date, today

KINDS = {"cold_water": ("💧", "Холодная вода", "м³"), "hot_water": ("🔥", "Горячая вода", "м³"),
         "electricity": ("⚡", "Электричество", "кВт·ч"), "gas": ("🔵", "Газ", "м³"),
         "heat": ("♨️", "Отопление", "Гкал"), "other": ("🔢", "Счётчик", "")}
MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


async def meters(family_id: int) -> list[dict]:
    rows = await db.get_pool().fetch(
        "SELECT m.*, (SELECT value FROM meter_readings r WHERE r.meter_id=m.id AND r.deleted_at IS NULL"
        "   ORDER BY r.created_at DESC LIMIT 1) AS last_value,"
        " (SELECT created_at FROM meter_readings r WHERE r.meter_id=m.id AND r.deleted_at IS NULL"
        "   ORDER BY r.created_at DESC LIMIT 1) AS last_at"
        f" FROM meters m WHERE m.family_id=$1 AND m.deleted_at IS NULL AND {scope.cond('meters', 'm.user_id')}"
        " ORDER BY m.kind, m.id", family_id)
    return [dict(r) for r in rows]


def _emoji(m: dict) -> str:
    return KINDS.get(m["kind"], KINDS["other"])[0]


async def add(user: User, data: dict) -> Reply:
    kind = data.get("kind") if data.get("kind") in KINDS else "other"
    title = str(data.get("title") or KINDS[kind][1]).strip()[:60]
    unit = str(data.get("unit") or KINDS[kind][2] or "").strip()[:10]
    verify = parse_date(data.get("verify_until"))
    pool = db.get_pool()
    same = await pool.fetchval("SELECT id FROM meters WHERE family_id=$1 AND lower(title)=lower($2) AND deleted_at IS NULL",
                               user.family_id, title)
    if same:
        await pool.execute("UPDATE meters SET verify_until=coalesce($2, verify_until), verify_reminded=NULL WHERE id=$1",
                           same, verify)
        head = "✏️ Обновил счётчик"
    else:
        same = await pool.fetchval("INSERT INTO meters (family_id, user_id, title, kind, unit, verify_until)"
                                   " VALUES ($1,$2,$3,$4,$5,$6) RETURNING id", user.family_id, user.id, title, kind, unit, verify)
        head = "🔢 Добавил счётчик"
    first = num(data.get("value"))
    if first is not None:
        await _insert(user, same, first)
    return Reply(text=f"{head}: {KINDS[kind][0]} <b>{esc(title)}</b>" + (f", поверка до {verify:%d.%m.%Y}" if verify else "")
                 + f"\nПоказания: <code>{esc(title.lower())} {fnum(first) if first is not None else '123,45'}</code> "
                   "или фото счётчика.\nБуду напоминать 20-го числа каждого месяца.",
                 buttons=[[Button(text="🔢 Все счётчики", action="r:0:list")]])


async def _insert(user: User, meter_id: int | None, value: float, attachment_id: int | None = None) -> int:
    rid = await db.get_pool().fetchval(
        "INSERT INTO meter_readings (family_id, meter_id, value, user_id, attachment_id) VALUES ($1,$2,$3,$4,$5) RETURNING id",
        user.family_id, meter_id, value, user.id, attachment_id)
    await db.get_pool().execute("INSERT INTO events (type, payload) VALUES ('meter.reading', $1)",
                                {"meter_id": meter_id, "value": value, "by": user.name})
    return rid


def _match(ms: list[dict], name: str | None, kind: str | None) -> list[dict]:
    n = (name or "").lower().replace("ё", "е").strip()
    if n:
        exact = [m for m in ms if m["title"].lower().replace("ё", "е") == n]
        if exact:
            return exact
        stems = [w[:max(3, len(w) - 2)] for w in n.replace("хол.", "холодн").replace("гор.", "горяч").split() if len(w) >= 3]
        by_words = [m for m in ms if stems and all(s in m["title"].lower().replace("ё", "е") for s in stems)]
        if by_words:
            return by_words
    if kind in KINDS:
        return [m for m in ms if m["kind"] == kind]
    return []


async def reading(user: User, items: list[dict], attachment_id: int | None = None) -> Reply:
    """items: [{meter, kind, value}]. Если счётчик не понятен — кнопки выбора."""
    ms = await meters(user.family_id)
    if not ms:
        return Reply(text="Сначала добавьте счётчики: <code>счётчик холодная вода кухня</code>, "
                          "<code>счётчик электричество, поверка до 2030-06-01</code>")
    lines, buttons = [], []
    for it in items[:8]:
        v = num(it.get("value"))
        if v is None or v < 0:
            continue
        found = _match(ms, it.get("meter"), it.get("kind"))
        if len(found) == 1:
            m = found[0]
            await _insert(user, m["id"], v, attachment_id)
            lines.append(await _saved_line(m, v))
        else:
            rid = await _insert(user, None, v, attachment_id)
            options = found or ms
            lines.append(f"❓ {fnum(v, 3)} — чей это счётчик?")
            buttons += [[Button(text=f"{_emoji(m)} {m['title'][:30]}", action=f"r:{rid}:m:{m['id']}")] for m in options[:6]]
    if not lines:
        return Reply(text="Не нашёл число показаний. Пример: <code>холодная вода 123,456</code>")
    text = "🔢 Показания\n" + "\n".join(lines)
    done = await _month_complete(user.family_id)
    if done and not buttons:
        text += "\n\n" + done
    return Reply(text=text, buttons=buttons)


async def _saved_line(m: dict, v: float) -> str:
    prev = await db.get_pool().fetchrow(
        "SELECT value, created_at FROM meter_readings WHERE meter_id=$1 AND deleted_at IS NULL"
        " ORDER BY created_at DESC OFFSET 1 LIMIT 1", m["id"])
    s = f"{_emoji(m)} {esc(m['title'])}: <b>{fnum(v, 3)}</b> {esc(m['unit'])}"
    if prev:
        diff = Decimal(str(v)) - Decimal(str(prev["value"]))
        if diff < 0:
            s += f"\n⚠️ Меньше прошлого ({fnum(prev['value'], 3)}). Проверьте; отменить — /history"
        else:
            s += f" · +{fnum(diff, 3)} с {prev['created_at'].astimezone(get_settings().zone):%d.%m}"
    return s


async def _month_complete(family_id: int, d: date | None = None) -> str:
    """Если по всем счётчикам есть показания за этот месяц — список для передачи."""
    d = d or today()
    ms = await meters(family_id)
    start = d.replace(day=1)
    vals = []
    for m in ms:
        r = await db.get_pool().fetchval(
            "SELECT value FROM meter_readings WHERE meter_id=$1 AND deleted_at IS NULL AND created_at >= $2"
            " ORDER BY created_at DESC LIMIT 1", m["id"], start)
        if r is None:
            return ""
        vals.append(f"{esc(m['title'])}: {fnum(r, 3)}")
    return f"✅ Все показания за {MONTHS[d.month - 1]} собраны:\n" + "\n".join(vals) if vals else ""


async def missing(family_id: int, d: date | None = None) -> list[dict]:
    d = d or today()
    return [m for m in await meters(family_id)
            if not m["last_at"] or m["last_at"].astimezone(get_settings().zone).date() < d.replace(day=1)]


async def list_reply(user: User, head: str = "") -> Reply:
    ms = await meters(user.family_id)
    if not ms:
        return Reply(text="🔢 Счётчиков нет. Добавьте: <code>счётчик холодная вода кухня, поверка до 2029-04-01</code>")
    text = (head + "\n\n" if head else "") + "🔢 <b>Счётчики</b>\n"
    for m in ms:
        text += f"\n{_emoji(m)} <b>{esc(m['title'])}</b>: "
        if m["last_value"] is not None:
            text += f"{fnum(m['last_value'], 3)} {esc(m['unit'])} ({m['last_at'].astimezone(get_settings().zone):%d.%m})"
            cons = await monthly(m["id"], 2)
            if cons:
                text += f" · за {MONTHS[cons[-1]['month'].month - 1]} {fnum(cons[-1]['used'], 2)}"
        else:
            text += "показаний ещё нет"
        if m["verify_until"]:
            left = (m["verify_until"] - today()).days
            text += f"\n   поверка до {m['verify_until']:%d.%m.%Y}" + (" ⚠️" if left <= 60 else "")
    miss = await missing(user.family_id)
    if miss:
        text += f"\n\nЗа этот месяц нет показаний: " + ", ".join(esc(m["title"]) for m in miss)
    return Reply(text=text)


async def monthly(meter_id: int, months: int = 12) -> list[dict]:
    """Расход по месяцам: разница последних показаний соседних месяцев."""
    rows = await db.get_pool().fetch(
        "SELECT DISTINCT ON (date_trunc('month', created_at AT TIME ZONE $3)) date_trunc('month', created_at AT TIME ZONE $3)::date AS m,"
        " value FROM meter_readings WHERE meter_id=$1 AND deleted_at IS NULL AND created_at > now() - make_interval(months => $2)"
        " ORDER BY date_trunc('month', created_at AT TIME ZONE $3), created_at DESC", meter_id, months + 1, get_settings().tz)
    out = []
    for a, b in zip(rows, rows[1:]):
        out.append({"month": b["m"], "value": float(b["value"]), "used": max(0.0, float(b["value"]) - float(a["value"]))})
    return out


async def handle_action(user: User, parts: list[str]) -> Reply:
    if parts[2] == "list":
        return await list_reply(user)
    if parts[2] == "m" and len(parts) > 3:
        rid, mid = int(parts[1]), int(parts[3])
        pool = db.get_pool()
        ok = await pool.fetchval("SELECT 1 FROM meters WHERE id=$1 AND family_id=$2 AND deleted_at IS NULL", mid, user.family_id)
        v = await pool.fetchval("UPDATE meter_readings SET meter_id=$2 WHERE id=$1 AND family_id=$3 AND meter_id IS NULL"
                                " RETURNING value", rid, mid, user.family_id) if ok else None
        if v is None:
            return Reply(text="Показание уже сохранено или устарело.")
        m = next(x for x in await meters(user.family_id) if x["id"] == mid)
        text = "🔢 " + await _saved_line(m, float(v))
        done = await _month_complete(user.family_id)
        return Reply(text=text + ("\n\n" + done if done else ""))
    return Reply(text="Неизвестное действие.")


async def send_due() -> None:
    n = now()
    if n.hour < get_settings().daily_reminder_hour:
        return
    d = n.date()
    pool = db.get_pool()
    for fam in await pool.fetch("SELECT DISTINCT family_id FROM meters WHERE deleted_at IS NULL"):
        fid = fam["family_id"]
        ms = await meters(fid)
        if not ms:
            continue
        first, last = min(m["submit_from"] for m in ms), min(m["submit_to"] for m in ms)
        miss = await missing(fid, d)
        for stage, day in ((1, first), (2, max(first + 1, last - 1))):
            if d.day < day or not miss:
                continue
            ok = await pool.fetchval("INSERT INTO meter_reminders (family_id, month, stage) VALUES ($1,$2,$3)"
                                     " ON CONFLICT DO NOTHING RETURNING stage", fid, d.replace(day=1), stage)
            if ok:
                author = ms[0]["user_id"]
                await notify.remind_about("meters", fid, author, Reply(
                    text=("🔢 <b>Пора передать показания счётчиков</b>" if stage == 1 else "🔢 <b>Напоминаю про показания</b>")
                         + f" (до {last}-го)\nНе хватает: " + ", ".join(f"{_emoji(m)} {esc(m['title'])}" for m in miss)
                         + "\n\nПришлите фото счётчика или напишите: <code>"
                         + esc(miss[0]["title"].lower()) + " 123,45</code>"))
        # Поверка: за 60 и 14 дней
        for m in ms:
            if not m["verify_until"]:
                continue
            left = (m["verify_until"] - d).days
            t = 14 if left <= 14 else 60 if left <= 60 else None
            if t is None or (m["verify_reminded"] is not None and m["verify_reminded"] <= t):
                continue
            await pool.execute("UPDATE meters SET verify_reminded=$2 WHERE id=$1", m["id"], t)
            await notify.remind_about("meters", fid, m["user_id"], Reply(
                text=f"🔧 Поверка счётчика {_emoji(m)} <b>{esc(m['title'])}</b> до {m['verify_until']:%d.%m.%Y} "
                     f"(осталось {left} дн.). Закажите поверку или замену заранее.\nПосле поверки: "
                     f"<code>счётчик {esc(m['title'].lower())}, поверка до ДД.ММ.ГГГГ</code>"))


async def morning_lines(user: User) -> list[str]:
    d = today()
    ms = await meters(user.family_id)
    if not ms or d.day < min(m["submit_from"] for m in ms) or d.day > max(m["submit_to"] for m in ms):
        return []
    miss = await missing(user.family_id, d)
    return [f"🔢 Показания счётчиков: не хватает {len(miss)} из {len(ms)}"] if miss else []
