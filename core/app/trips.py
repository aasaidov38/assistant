"""Режим поездки: даты, бюджет, траты поездки, чек-лист сборов, погода по маршруту.

Траты, сделанные в даты поездки, относятся к ней сами (кроме регулярных платежей).
Погода по точкам маршрута — Open-Meteo (бесплатно, без ключа): координаты по названию,
прогноз на даты поездки, если до неё не больше 16 дней.
Напоминания: за 3 дня (сборы + погода) и накануне (погода по маршруту).
Кнопки: j:<item_id>:c (отметить пункт сборов) | j:<trip_id>:show | j:<trip_id>:pack | j:0:list
"""
import logging
from datetime import date, timedelta
from decimal import Decimal

import httpx

from . import db, finance, notify, scope
from .config import get_settings
from .morning import WMO
from .schemas import Button, Reply
from .users import User
from .util import esc, now, num, parse_date, plural, today

log = logging.getLogger("trips")
_client = httpx.AsyncClient(timeout=10)

KIND = {"car": "🚗", "moto": "🏍", "plane": "✈️", "train": "🚆", "other": "🧳"}

CHECKLIST = {
    "common": ["Документы и банковские карты", "Наличные", "Зарядки и пауэрбанк", "Аптечка и свои лекарства",
               "Вода и перекус в дорогу", "Детям: игры, наушники, сменная одежда"],
    "moto": ["Права, СТС, ОСАГО", "Шлем и визор (чистый)", "Мотоэкипировка и мотоботы", "Запасные перчатки",
             "Дождевик", "Набор инструментов", "Ремкомплект шин и насос", "Смазка для цепи",
             "Сетка/ремни для багажа", "Проверить: давление, цепь, тормоза, масло"],
    "car": ["Права, СТС, ОСАГО", "Аптечка, огнетушитель, знак аварийной остановки", "Трос и провода прикуривателя",
            "Насос, запаска в порядке", "Омывайка", "Проверить: масло, давление, свет"],
    "plane": ["Паспорта и билеты", "Распечатка брони жилья", "Переходник для розетки", "Жидкости ≤100 мл в прозрачном пакете"],
    "train": ["Паспорта и билеты", "Тапочки и кружка", "Влажные салфетки"],
    "other": [],
}


async def _geocode(name: str) -> tuple[float, float] | None:
    try:
        r = await _client.get("https://geocoding-api.open-meteo.com/v1/search",
                              params={"name": name, "count": 1, "language": "ru", "format": "json"})
        r.raise_for_status()
        res = (r.json().get("results") or [None])[0]
        return (res["latitude"], res["longitude"]) if res else None
    except Exception as e:
        log.warning("Геокодер: %s", e)
        return None


async def trips(family_id: int, include_past: bool = False) -> list[dict]:
    rows = await db.get_pool().fetch(
        "SELECT t.*, (SELECT coalesce(sum(x.amount),0) FROM transactions x WHERE x.trip_id=t.id AND x.deleted_at IS NULL"
        "   AND x.kind='expense') AS spent,"
        " (SELECT count(*) FROM trip_checklist c WHERE c.trip_id=t.id) AS items,"
        " (SELECT count(*) FROM trip_checklist c WHERE c.trip_id=t.id AND c.done) AS packed"
        f" FROM trips t WHERE t.family_id=$1 AND t.deleted_at IS NULL AND {scope.cond('trips', 't.user_id')}" + ("" if include_past else " AND t.end_on >= $2")
        + " ORDER BY t.start_on", family_id, *([] if include_past else [today()]))
    return [dict(r) for r in rows]


async def current(family_id: int) -> dict | None:
    """Идущая сейчас поездка, иначе ближайшая будущая."""
    ts = await trips(family_id)
    d = today()
    return next((t for t in ts if t["start_on"] <= d <= t["end_on"]), None) or (ts[0] if ts else None)


async def places(trip_id: int) -> list[dict]:
    return [dict(r) for r in await db.get_pool().fetch("SELECT * FROM trip_places WHERE trip_id=$1 ORDER BY sort, id", trip_id)]


async def checklist(trip_id: int) -> list[dict]:
    return [dict(r) for r in await db.get_pool().fetch("SELECT * FROM trip_checklist WHERE trip_id=$1 ORDER BY done, sort, id", trip_id)]


async def create(user: User, data: dict) -> Reply:
    title = str(data.get("title") or "").strip()[:80]
    start, end = parse_date(data.get("start_on")), parse_date(data.get("end_on"))
    if not title or not start:
        return Reply(text="Пример: <code>поездка на Байкал с 5 по 12 июля на мото, бюджет 60000, маршрут Иркутск — Листвянка — Ольхон</code>")
    end = end if end and end >= start else start
    kind = data.get("kind") if data.get("kind") in KIND else "car"
    pool = db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        tid = await conn.fetchval("INSERT INTO trips (family_id, user_id, title, kind, start_on, end_on, budget)"
                                  " VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id", user.family_id, user.id, title, kind,
                                  start, end, num(data.get("budget")))
        for i, item in enumerate(CHECKLIST[kind] + CHECKLIST["common"]):
            await conn.execute("INSERT INTO trip_checklist (trip_id, title, sort) VALUES ($1,$2,$3)", tid, item, i)
        for i, item in enumerate(data.get("extra_items") or []):
            await conn.execute("INSERT INTO trip_checklist (trip_id, title, sort) VALUES ($1,$2,$3)", tid, str(item)[:100], 100 + i)
        # траты, уже сделанные в эти даты (например, бронь заранее записана датой поездки), тоже относятся к ней
        await conn.execute("UPDATE transactions SET trip_id=$1 WHERE family_id=$2 AND trip_id IS NULL AND deleted_at IS NULL"
                           " AND source <> 'bill' AND occurred_on BETWEEN $3 AND $4", tid, user.family_id, start, end)
        await db.emit(conn, "trip.created", {"id": tid, "title": title, "start": start.isoformat(), "end": end.isoformat()})
    await set_route(tid, data.get("route") or [])
    return await show(user, tid, head="🧳 Поездка создана. Траты в эти даты буду относить к ней сами.")


async def set_route(trip_id: int, names: list) -> None:
    pool = db.get_pool()
    await pool.execute("DELETE FROM trip_places WHERE trip_id=$1", trip_id)
    for i, n in enumerate([str(x).strip()[:80] for x in names if str(x).strip()][:12]):
        ll = await _geocode(n)
        await pool.execute("INSERT INTO trip_places (trip_id, name, lat, lon, sort) VALUES ($1,$2,$3,$4,$5)",
                           trip_id, n, ll[0] if ll else None, ll[1] if ll else None, i)


async def weather(trip: dict) -> list[dict]:
    """Прогноз по точкам маршрута на даты поездки (если до неё ≤ 16 дней)."""
    d = today()
    if trip["start_on"] > d + timedelta(days=15) or trip["end_on"] < d:
        return []
    start, end = max(trip["start_on"], d), min(trip["end_on"], d + timedelta(days=15))
    out = []
    for p in await places(trip["id"]):
        if p["lat"] is None:
            continue
        try:
            r = await _client.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": p["lat"], "longitude": p["lon"], "timezone": get_settings().tz, "wind_speed_unit": "ms",
                "start_date": start.isoformat(), "end_date": end.isoformat(),
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max"})
            r.raise_for_status()
            j = r.json()["daily"]
        except Exception as e:
            log.warning("Погода для %s: %s", p["name"], e)
            continue
        out.append({"place": p["name"], "days": [
            {"date": j["time"][i], "code": j["weather_code"][i], "icon": WMO.get(j["weather_code"][i], "🌡").split()[0],
             "min": j["temperature_2m_min"][i], "max": j["temperature_2m_max"][i], "rain": j["precipitation_sum"][i],
             "wind": j["wind_speed_10m_max"][i]} for i in range(len(j.get("time") or []))]})
    return out


def _t(v) -> str:
    v = round(v)
    return f"+{v}" if v > 0 else str(v).replace("-", "−")


def weather_text(w: list[dict], moto: bool = False) -> str:
    if not w:
        return ""
    lines = ["🌤 <b>Погода по маршруту</b>"]
    warn = []
    for p in w:
        days = " · ".join(f"{date.fromisoformat(d['date']):%d.%m} {d['icon']} {_t(d['min'])}…{_t(d['max'])}" for d in p["days"][:5])
        lines.append(f"{esc(p['place'])}: {days}")
        for d in p["days"]:
            if (d["rain"] or 0) >= 5:
                warn.append(f"{date.fromisoformat(d['date']):%d.%m} дождь в {esc(p['place'])} ({d['rain']:.0f} мм)")
            if moto and (d["wind"] or 0) >= 12:
                warn.append(f"{date.fromisoformat(d['date']):%d.%m} сильный ветер в {esc(p['place'])} ({d['wind']:.0f} м/с)")
    if warn:
        lines.append("⚠️ " + "; ".join(warn[:4]))
    return "\n".join(lines)


def _status(t: dict) -> str:
    d = today()
    days = (t["end_on"] - t["start_on"]).days + 1
    if d < t["start_on"]:
        return f"через {plural((t['start_on'] - d).days, 'день', 'дня', 'дней')}"
    if d > t["end_on"]:
        return "завершена"
    return f"день {(d - t['start_on']).days + 1} из {days}"


async def by_category(trip_id: int) -> list[dict]:
    rows = await db.get_pool().fetch(
        "SELECT c.name, c.emoji, sum(x.amount) AS s FROM transactions x JOIN categories c ON c.id=x.category_id"
        " WHERE x.trip_id=$1 AND x.deleted_at IS NULL AND x.kind='expense' GROUP BY 1,2 ORDER BY 3 DESC", trip_id)
    return [dict(r) for r in rows]


async def show(user: User, trip_id: int | None = None, head: str = "") -> Reply:
    t = next((x for x in await trips(user.family_id, True) if x["id"] == trip_id), None) if trip_id else await current(user.family_id)
    if not t:
        return Reply(text=(head + "\n\n" if head else "") + "🧳 Поездок не запланировано.\n"
                     "Пример: <code>поездка на Байкал с 5 по 12 июля на мото, бюджет 60000, маршрут Иркутск — Листвянка — Ольхон</code>")
    days = (t["end_on"] - t["start_on"]).days + 1
    text = (head + "\n\n" if head else "") + (f"{KIND[t['kind']]} <b>{esc(t['title'])}</b>\n"
                                               f"{t['start_on']:%d.%m}–{t['end_on']:%d.%m.%Y} · {plural(days, 'день', 'дня', 'дней')} · {_status(t)}")
    pl = await places(t["id"])
    if pl:
        text += "\n📍 " + " → ".join(esc(p["name"]) for p in pl)
    spent = Decimal(str(t["spent"]))
    if t["budget"]:
        left = Decimal(str(t["budget"])) - spent
        text += f"\n💰 Потрачено {finance.money(spent)} из {finance.money(t['budget'])}" + \
                (f" · осталось {finance.money(left)}" if left >= 0 else f" · перерасход {finance.money(-left)}")
        d = today()
        if t["start_on"] <= d <= t["end_on"] and left > 0:
            rest = (t["end_on"] - d).days + 1
            text += f" (≈ {finance.money((left / rest).quantize(Decimal('1')))} в день)"
    elif spent:
        text += f"\n💰 Потрачено {finance.money(spent)}"
    cats = await by_category(t["id"])
    if cats:
        text += "\n" + " · ".join(f"{c['emoji']} {esc(c['name'])} {finance.money(c['s'])}" for c in cats[:5])
    if t["items"]:
        text += f"\n🎒 Сборы: {t['packed']} из {t['items']}"
    w = weather_text(await weather(t), t["kind"] == "moto")
    if w:
        text += "\n\n" + w
    buttons = [[Button(text="🎒 Чек-лист сборов", action=f"j:{t['id']}:pack")]] if t["items"] else []
    return Reply(text=text[:4000], buttons=buttons)


async def pack_reply(user: User, trip_id: int) -> Reply:
    t = next((x for x in await trips(user.family_id, True) if x["id"] == trip_id), None)
    if not t:
        return Reply(text="Поездка не найдена.")
    items = await checklist(trip_id)
    text = f"🎒 <b>Сборы: {esc(t['title'])}</b> — {sum(i['done'] for i in items)} из {len(items)}\n" + \
           "\n".join(("✅ " if i["done"] else "▫️ ") + esc(i["title"]) for i in items) + \
           "\n\nНажмите пункт, чтобы отметить. Добавить: <code>в сборы: дождевик</code>"
    buttons = [[Button(text=("✅ " if i["done"] else "▫️ ") + i["title"][:40], action=f"j:{i['id']}:c")] for i in items[:24]]
    return Reply(text=text, buttons=buttons)


async def add_items(user: User, items: list) -> Reply:
    t = await current(user.family_id)
    if not t:
        return Reply(text="Сначала создайте поездку: <code>поездка на Байкал с 5 по 12 июля</code>")
    for it in [str(x).strip()[:100] for x in items if str(x).strip()][:20]:
        await db.get_pool().execute("INSERT INTO trip_checklist (trip_id, title, sort) VALUES ($1,$2,200)", t["id"], it)
    return await pack_reply(user, t["id"])


async def toggle(user: User, item_id: int) -> dict | None:
    r = await db.get_pool().fetchrow(
        "UPDATE trip_checklist c SET done = NOT done FROM trips t WHERE c.id=$1 AND t.id=c.trip_id AND t.family_id=$2"
        " RETURNING c.trip_id, c.done", item_id, user.family_id)
    return dict(r) if r else None


async def delete(user: User, trip_id: int) -> bool:
    async with db.get_pool().acquire() as conn, conn.transaction():
        n = await conn.execute("UPDATE trips SET deleted_at=now() WHERE id=$1 AND family_id=$2 AND deleted_at IS NULL",
                               trip_id, user.family_id)
        await conn.execute("UPDATE transactions SET trip_id=NULL WHERE trip_id=$1", trip_id)
    return n.endswith("1")


async def handle_action(user: User, parts: list[str]) -> Reply:
    op = parts[2]
    if op == "list":
        return await show(user)
    obj = int(parts[1])
    if op == "show":
        return await show(user, obj)
    if op == "pack":
        return await pack_reply(user, obj)
    if op == "c":
        r = await toggle(user, obj)
        return await pack_reply(user, r["trip_id"]) if r else Reply(text="Пункт не найден.")
    return Reply(text="Неизвестное действие.")


async def send_due() -> None:
    n = now()
    if n.hour < get_settings().daily_reminder_hour:
        return
    d = n.date()
    for r in await db.get_pool().fetch("SELECT * FROM trips WHERE deleted_at IS NULL AND start_on BETWEEN $1 AND $2",
                                       d, d + timedelta(days=3)):
        t = dict(r)
        left = (t["start_on"] - d).days
        key = "before1" if left <= 1 else "before3"
        if key in t["reminded"]:
            continue
        ok = await db.get_pool().fetchval("UPDATE trips SET reminded = array_append(reminded, $2) WHERE id=$1"
                                          " AND NOT ($2 = ANY(reminded)) RETURNING id", t["id"], key)
        if not ok:
            continue
        user = User(id=t["user_id"], family_id=t["family_id"], name="", kcal_goal=None)
        reply = await show(user, t["id"], head="🧳 <b>Завтра в путь!</b>" if left <= 1 else f"🧳 <b>Поездка через {left} дн.</b>")
        await notify.remind(t["family_id"], t["user_id"], reply, topic="trips", section="trips")


async def morning_lines(user: User) -> list[str]:
    t = await current(user.family_id)
    if not t or not (t["start_on"] - timedelta(days=7) <= today() <= t["end_on"]):
        return []
    s = f"🧳 {esc(t['title'])}: {_status(t)}"
    if t["budget"]:
        s += f", потрачено {finance.money(t['spent'])} из {finance.money(t['budget'])}"
    if today() < t["start_on"] and t["items"]:
        s += f", собрано {t['packed']} из {t['items']}"
    return [s]
