"""Утренняя сводка: погода, дела на сегодня, вчерашнее питание, бюджет, покупки, привычки.

Погода берётся с open-meteo.com: бесплатно и без ключа. Место — базовый город человека (⚙️ Настройки), а если он
не выбран, общий из .env (WEATHER_LAT, WEATHER_LON). С 1.10 в сводке есть строка о магнитной буре (при Kp от 5).
"""
import logging
import re
from datetime import datetime, time, timedelta

import httpx

from . import budgets, db, envdata, finance, habits, notify, nutrition, prefs, scope, shopping, users
from .config import get_settings
from .schemas import Reply
from .util import WEEKDAYS, esc, now, today

log = logging.getLogger("morning")
_client = httpx.AsyncClient(timeout=10)

WMO = {0: "☀️ ясно", 1: "🌤 преимущественно ясно", 2: "⛅ переменная облачность", 3: "☁️ пасмурно",
       45: "🌫 туман", 48: "🌫 изморозь", 51: "🌦 морось", 53: "🌦 морось", 55: "🌦 сильная морось",
       56: "🌧 ледяная морось", 57: "🌧 ледяная морось", 61: "🌧 небольшой дождь", 63: "🌧 дождь",
       65: "🌧 сильный дождь", 66: "🌧 ледяной дождь", 67: "🌧 ледяной дождь", 71: "🌨 небольшой снег",
       73: "🌨 снег", 75: "❄️ сильный снег", 77: "🌨 снежная крупа", 80: "🌦 ливень", 81: "🌧 ливни",
       82: "⛈ сильные ливни", 85: "🌨 снегопад", 86: "❄️ сильный снегопад", 95: "⛈ гроза",
       96: "⛈ гроза с градом", 99: "⛈ сильная гроза с градом"}


_cache: dict = {}
_offsets: dict = {}      # смещение местного времени от UTC по местам, секунды


def _default_loc() -> dict:
    s = get_settings()
    return {"name": s.weather_place, "lat": s.weather_lat, "lon": s.weather_lon, "tz": s.tz, "custom": False}


async def forecast(loc: dict | None = None) -> dict | None:
    """Погода сейчас, по часам на сутки и на 7 дней для места loc. Кешируется на 15 минут для каждого места."""
    import time as _t
    loc = loc or _default_loc()
    key = (round(loc["lat"], 2), round(loc["lon"], 2))
    c0 = _cache.get(key)
    if c0 and _t.time() - c0[0] < 900:
        return c0[1]
    s = get_settings()
    try:
        r = await _client.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto" if loc.get("custom") else s.tz,
            "forecast_days": 7,
            "wind_speed_unit": "ms",
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m,precipitation",
            "hourly": "temperature_2m,weather_code,precipitation_probability",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max,sunrise,sunset"})
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        log.warning("Погода недоступна: %s", e)
        return None
    c, d, h = j.get("current") or {}, j.get("daily") or {}, j.get("hourly") or {}
    now_s = (c.get("time") or now().strftime("%Y-%m-%dT%H:00"))[:13] + ":00"       # местное время выбранного города
    times = h.get("time") or []
    i0 = next((i for i, t in enumerate(times) if t >= now_s), 0)
    data = {
        "place": loc["name"], "utc_offset": j.get("utc_offset_seconds") or 0,
        "now": {"temp": c.get("temperature_2m"), "feels": c.get("apparent_temperature"), "code": c.get("weather_code"),
                "text": WMO.get(c.get("weather_code"), ""), "wind": c.get("wind_speed_10m"),
                "humidity": c.get("relative_humidity_2m")},
        "hours": [{"time": times[i][11:16], "temp": h["temperature_2m"][i], "code": h["weather_code"][i],
                   "icon": WMO.get(h["weather_code"][i], "🌡").split()[0],
                   "rain": (h.get("precipitation_probability") or [None] * len(times))[i]}
                  for i in range(i0, min(i0 + 24, len(times)), 3)],
        "days": [{"date": d["time"][i], "min": d["temperature_2m_min"][i], "max": d["temperature_2m_max"][i],
                  "code": d["weather_code"][i], "text": WMO.get(d["weather_code"][i], ""),
                  "rain": d["precipitation_sum"][i], "wind": d["wind_speed_10m_max"][i],
                  "sunrise": (d.get("sunrise") or [""] * 7)[i][11:16], "sunset": (d.get("sunset") or [""] * 7)[i][11:16]}
                 for i in range(len(d.get("time") or []))],
    }
    _cache[key] = (_t.time(), data)
    return data


async def weather(loc: dict | None = None) -> str:
    s = get_settings()
    loc = loc or _default_loc()
    try:
        r = await _client.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto" if loc.get("custom") else s.tz, "forecast_days": 1,
            "wind_speed_unit": "ms",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max"})
        r.raise_for_status()
        j = r.json()
        _offsets[(round(loc["lat"], 2), round(loc["lon"], 2))] = j.get("utc_offset_seconds") or 0
        d = {k: v[0] for k, v in j["daily"].items()}
    except Exception as e:
        log.warning("Погода недоступна: %s", e)
        return ""
    t1, t2 = round(d["temperature_2m_min"]), round(d["temperature_2m_max"])
    text = f"{WMO.get(d['weather_code'], '🌡')}, {_t(t1)}…{_t(t2)} °C"
    if d.get("precipitation_sum"):
        text += f", осадки {d['precipitation_sum']:.0f} мм"
    if d.get("wind_speed_10m_max"):
        text += f", ветер до {d['wind_speed_10m_max']:.0f} м/с"
    hint = ""
    if t1 <= -20:
        hint = " 🧣 очень холодно"
    elif d["weather_code"] >= 51 and d["weather_code"] not in (71, 73, 75, 77, 85, 86):
        hint = " ☂️ возьмите зонт"
    return f"{esc(loc['name'])}: {text}{hint}"


async def storm_line(user_id: int, offset_s: int = 0) -> str:
    """Строка о магнитной буре на сегодня и завтра — только если Kp от 5 и человек не выключил тему «storm»."""
    if not await prefs.topic_on(user_id, "storm"):
        return ""
    k = await envdata.kp(offset_s)
    if not k:
        return ""
    names = ["сегодня", "завтра"]
    parts = [f"{names[i]} до Kp {d['max']:g} ({d['label']}, пик около {d['peak']})"
             for i, d in enumerate(k["days"][:2]) if d["storm"]]
    return ("🧲 Магнитная буря: " + "; ".join(parts)) if parts else ""


def _offset(loc: dict) -> int:
    """Смещение местного времени города от UTC, секунды (для выбранного города — из кэша прогноза)."""
    k = (round(loc["lat"], 2), round(loc["lon"], 2))
    if k in _offsets:
        return _offsets[k]
    c = _cache.get(k)
    if c:
        return c[1].get("utc_offset") or 0
    return int(now().utcoffset().total_seconds()) if not loc.get("custom") else 0


def _t(v: int) -> str:
    return f"+{v}" if v > 0 else str(v).replace("-", "−")


async def build(user: users.User) -> Reply:
    async with scope.as_user(user):
        return await _build(user)


async def _build(user: users.User) -> Reply:
    d = today()
    text = f"☀️ <b>Доброе утро, {esc(user.name)}!</b>\n{WEEKDAYS[d.weekday()].capitalize()}, {d:%d.%m.%Y}\n"
    loc = await prefs.location(user.id)
    w = await weather(loc)
    if w:
        text += f"\n{w}\n"
        try:
            st = await storm_line(user.id, _offset(loc))
        except Exception:
            log.exception("Строка о магнитной буре")
            st = ""
        if st:
            text += f"{st}\n"

    z = get_settings().zone
    a = datetime.combine(d, time.min, tzinfo=z)
    pool = db.get_pool()
    rows = await pool.fetch(
        "SELECT title, due_at, all_day FROM tasks WHERE family_id=$1 AND status='open'"
        " AND coalesce(assignee_id, user_id)=$2 AND due_at < $3 ORDER BY due_at", user.family_id, user.id,
        a + timedelta(days=1))
    today_t = [r for r in rows if r["due_at"] >= a]
    late = [r for r in rows if r["due_at"] < a]
    if today_t or late:
        text += "\n📝 <b>Дела на сегодня</b>\n"
        for r in today_t[:8]:
            at = "" if r["all_day"] else f"{r['due_at'].astimezone(z):%H:%M} "
            text += f"• {at}{esc(r['title'])}\n"
        if late:
            text += f"⚠️ Просрочено: {len(late)} — " + ", ".join(esc(r["title"]) for r in late[:3]) + "\n"
    else:
        text += "\n📝 На сегодня дел нет.\n"

    y = await nutrition.day_totals(user, d - timedelta(days=1))
    if y["n"]:
        goal = nutrition.goal_for(user)
        text += f"\n🍽 Вчера: {y['kcal']:.0f} из {goal} ккал · Б {y['protein']:.0f} · Ж {y['fat']:.0f} · У {y['carbs']:.0f}\n"

    hs = []
    g = await habits.goals(user.id)
    for kind in ("water", "steps", "sleep"):
        v = await habits.day_value(user.id, kind, d - timedelta(days=1) if kind != "sleep" else d)
        if v:
            e = habits.KINDS[kind][0]
            hs.append(f"{e} {habits.fmt(kind, v)}" + (f" из {habits.fmt(kind, g[kind])}" if g.get(kind) else ""))
    wv = await habits.last_weight(user.id)
    if wv is not None:
        hs.append(f"⚖️ {habits.fmt('weight', wv)}")
    if hs:
        text += "📈 " + " · ".join(hs) + "\n"

    warn = [r for r in await budgets.status_rows(user.family_id) if r["spent"] >= r["lim"] * 8 / 10]
    if warn:
        text += "\n🎯 <b>Лимиты</b>\n" + "".join(
            f"{'🟥' if r['spent'] >= r['lim'] else '🟨'} {esc(r['name'])}: {finance.money(r['spent'])} из "
            f"{finance.money(r['lim'])}\n" for r in warn)
    spent, _n = await finance.today_expenses(user, d - timedelta(days=1))
    if spent:
        text += f"💸 Вчера {'вы потратили' if scope.is_personal('finance') else 'семья потратила'} {finance.money(spent)}\n"

    # Платежи, документы, счётчики, гараж, лекарства (версия 1.5)
    from . import bills, debts, garage, meds, meters, papers, trips
    extra = []
    for mod in (trips, meds, bills, debts, papers, meters, garage):
        try:
            extra += await mod.morning_lines(user)
        except Exception:
            log.exception("Утренняя сводка: %s", mod.__name__)
    if extra:
        text += "\n" + "\n".join(extra) + "\n"

    shop = await shopping.open_items(user.family_id)
    if shop:
        text += f"\n🛒 В списке покупок: {len(shop)} — " + ", ".join(esc(s["title"]) for s in shop[:6]) + \
                (" …" if len(shop) > 6 else "") + "\n"
    return Reply(text=text[:4000])


def _user_time(row) -> time:
    if row["morning_time"]:
        return row["morning_time"]
    h, m = (get_settings().morning_time.split(":") + ["0"])[:2]
    return time(int(h), int(m))


async def send_due() -> None:
    """Раз в 30 секунд: кому пора отправить сводку. Если ядро было выключено, догоняет в течение 3 часов."""
    n = now()
    rows = await db.get_pool().fetch(
        "SELECT id, morning_time FROM users WHERE morning_enabled AND (morning_sent_on IS NULL OR morning_sent_on < $1)",
        n.date())
    for r in rows:
        t = datetime.combine(n.date(), _user_time(r), tzinfo=n.tzinfo)
        if not (t <= n < t + timedelta(hours=3)):
            continue
        done = await db.get_pool().fetchval(
            "UPDATE users SET morning_sent_on=$2 WHERE id=$1 AND (morning_sent_on IS NULL OR morning_sent_on < $2)"
            " RETURNING id", r["id"], n.date())
        if not done:
            continue
        user = await users.get(r["id"])
        try:
            await notify.send(user.id, await build(user), urgent=True)
        except Exception:
            log.exception("Утренняя сводка для %s", user.id)


async def command(user: users.User, cmd: str) -> Reply:
    """/morning — сводка сейчас; /morning 7:00 — время; /morning off|on."""
    pool = db.get_pool()
    m = re.search(r"(\d{1,2})[:.](\d{2})|\b(\d{1,2})\b", cmd)
    if "off" in cmd or "выкл" in cmd:
        await pool.execute("UPDATE users SET morning_enabled=false WHERE id=$1", user.id)
        return Reply(text="☀️ Утренняя сводка выключена. Включить: /morning on")
    if m:
        h, mi = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(3)), 0)
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            return Reply(text="Укажите время так: <code>/morning 7:00</code>")
        await pool.execute("UPDATE users SET morning_enabled=true, morning_time=$2 WHERE id=$1", user.id, time(h, mi))
        return Reply(text=f"☀️ Утренняя сводка будет приходить в {h}:{mi:02d}. Выключить: /morning off")
    if "on" in cmd or "вкл" in cmd:
        await pool.execute("UPDATE users SET morning_enabled=true WHERE id=$1", user.id)
        return Reply(text="☀️ Утренняя сводка включена. Время: <code>/morning 7:00</code>")
    return await build(user)
