"""Личные настройки (версия 1.7): оформление панели, вкладки, уведомления, тихие часы, сводки, цели.

Хранятся в users.settings (jsonb) и в старых столбцах users (утренняя сводка, недельный разбор, цель калорий),
цели привычек — в metric_goals, уведомления TickTick — в интеграции TickTick. Здесь всё собрано в одно место:
full(user) — всё для панели, update(user, patch) — сохранить изменения.
"""
import re
from datetime import datetime, time, timedelta
from decimal import Decimal

from . import db, ticktick, users
from .config import get_settings
from .util import now

TOPICS = {
    "bills":  "💳 Регулярные платежи",
    "papers": "📄 Документы, полисы, гарантии",
    "meters": "🔢 Показания счётчиков",
    "garage": "🏍 ТО, пробег и сезонные работы",
    "meds":   "💊 Лекарства",
    "debts":  "🤝 Долги",
    "trips":  "🧳 Поездки",
    "tasks":  "📝 Выполнение дел, которые вы поручили",
    "budget": "🎯 Лимиты и траты других",
    "updates": "🆕 Новые версии и «что нового»",
}
TABS = {"fin": "💰 Финансы", "food": "🍽 Питание", "hab": "📈 Здоровье", "tasks": "📝 Дела", "shop": "🛒 Покупки",
        "house": "🏠 Дом", "garage": "🏍 Гараж", "trips": "🧳 Поездки", "debts": "🤝 Долги", "cards": "💳 Карты", "hist": "🕘 История",
        "ask": "🔎 Спросить"}
DEFAULTS = {"theme": "system", "animations": True, "hidden_tabs": [],
            "notify": {k: True for k in TOPICS},
            "quiet": {"on": False, "from": "22:00", "to": "08:00"},
            "mirror": True}
GOALS = {"kcal": ("🔥 Калории", "ккал"), "water": ("💧 Вода", "мл"), "steps": ("👟 Шаги", ""),
         "weight": ("⚖️ Целевой вес", "кг"), "sleep": ("😴 Сон", "ч")}
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _merge(raw: dict | None) -> dict:
    raw = raw or {}
    out = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    out["notify"] = {**DEFAULTS["notify"], **{k: bool(v) for k, v in (raw.get("notify") or {}).items() if k in TOPICS}}
    out["quiet"] = {**DEFAULTS["quiet"], **(raw.get("quiet") or {})}
    out["hidden_tabs"] = [t for t in out.get("hidden_tabs") or [] if t in TABS]
    return out


async def get(user_id: int) -> dict:
    raw = await db.get_pool().fetchval("SELECT settings FROM users WHERE id=$1", user_id)
    if isinstance(raw, str):
        import json
        raw = json.loads(raw or "{}")
    return _merge(raw)


def _parse_time(v) -> time | None:
    m = _TIME.match(str(v or "").strip())
    return time(int(m.group(1)), int(m.group(2))) if m else None


def quiet_until(s: dict, at: datetime | None = None) -> datetime | None:
    """Если сейчас тихие часы — когда они закончатся, иначе None."""
    q = s.get("quiet") or {}
    if not q.get("on"):
        return None
    a, b = _parse_time(q.get("from")), _parse_time(q.get("to"))
    if not a or not b or a == b:
        return None
    n = at or now()
    t = n.timetz().replace(tzinfo=None)
    inside = (a <= t < b) if a < b else (t >= a or t < b)
    if not inside:
        return None
    end = datetime.combine(n.date(), b, tzinfo=n.tzinfo)
    return end if end > n else end + timedelta(days=1)


async def full(user: users.User) -> dict:
    """Все настройки человека для панели."""
    pool = db.get_pool()
    s = await get(user.id)
    row = await pool.fetchrow("SELECT morning_enabled, morning_time, weekly_digest, digest_day, digest_hour, kcal_goal"
                              " FROM users WHERE id=$1", user.id)
    cfg = get_settings()
    mt = row["morning_time"]
    if not mt:
        h, m = (cfg.morning_time.split(":") + ["0"])[:2]
        mt = time(int(h), int(m))
    goals = {r["kind"]: float(r["target"]) for r in
             await pool.fetch("SELECT kind, target FROM metric_goals WHERE user_id=$1", user.id)}
    goals["kcal"] = row["kcal_goal"] or cfg.default_kcal_goal
    tt = await ticktick.token_info(user.id)
    dd = row["digest_day"] if row["digest_day"] is not None else cfg.weekly_digest_day
    return {**s,
            "morning": {"on": row["morning_enabled"], "time": mt.strftime("%H:%M")},
            "digest": {"on": row["weekly_digest"] and dd >= 0, "day": max(dd, 0) if dd is not None else 6,
                       "hour": row["digest_hour"] if row["digest_hour"] is not None else cfg.weekly_digest_hour},
            "ticktick": {"connected": tt.get("connected", False) or bool(tt.get("expires_at")),
                         "notify": tt.get("notify", True)},
            "goals": goals}


async def update(user: users.User, patch: dict) -> dict:
    """Сохраняет изменения. Неизвестные ключи и неверные значения пропускаются."""
    pool = db.get_pool()
    s = await get(user.id)
    p = patch or {}
    if p.get("theme") in ("system", "light", "dark"):
        s["theme"] = p["theme"]
    if "animations" in p:
        s["animations"] = bool(p["animations"])
    if isinstance(p.get("hidden_tabs"), list):
        s["hidden_tabs"] = [t for t in p["hidden_tabs"] if t in TABS]
    if isinstance(p.get("notify"), dict):
        s["notify"].update({k: bool(v) for k, v in p["notify"].items() if k in TOPICS})
    if isinstance(p.get("quiet"), dict):
        q = p["quiet"]
        if "on" in q:
            s["quiet"]["on"] = bool(q["on"])
        for k in ("from", "to"):
            if k in q and _parse_time(q[k]):
                t = _parse_time(q[k])
                s["quiet"][k] = f"{t.hour:02d}:{t.minute:02d}"
    if "mirror" in p:
        s["mirror"] = bool(p["mirror"])
    await pool.execute("UPDATE users SET settings=$2::jsonb WHERE id=$1", user.id, s)

    if isinstance(p.get("morning"), dict):
        m = p["morning"]
        if "on" in m:
            await pool.execute("UPDATE users SET morning_enabled=$2 WHERE id=$1", user.id, bool(m["on"]))
        if _parse_time(m.get("time")):
            await pool.execute("UPDATE users SET morning_time=$2 WHERE id=$1", user.id, _parse_time(m["time"]))
    if isinstance(p.get("digest"), dict):
        d = p["digest"]
        if "on" in d:
            await pool.execute("UPDATE users SET weekly_digest=$2 WHERE id=$1", user.id, bool(d["on"]))
        if str(d.get("day", "")).lstrip("-").isdigit() and 0 <= int(d["day"]) <= 6:
            await pool.execute("UPDATE users SET digest_day=$2 WHERE id=$1", user.id, int(d["day"]))
        if str(d.get("hour", "")).isdigit() and 0 <= int(d["hour"]) <= 23:
            await pool.execute("UPDATE users SET digest_hour=$2 WHERE id=$1", user.id, int(d["hour"]))
    if isinstance(p.get("ticktick"), dict) and "notify" in p["ticktick"]:
        await ticktick.save_sync_state(user.id, notify=bool(p["ticktick"]["notify"]))
    if isinstance(p.get("goals"), dict):
        for k, v in p["goals"].items():
            if k not in GOALS:
                continue
            try:
                val = Decimal(str(v)) if v not in (None, "") else Decimal(0)
            except Exception:
                continue
            if k == "kcal":
                await pool.execute("UPDATE users SET kcal_goal=$2 WHERE id=$1", user.id,
                                   int(val) if 500 <= val <= 10000 else None)
            elif val <= 0:
                await pool.execute("DELETE FROM metric_goals WHERE user_id=$1 AND kind=$2", user.id, k)
            else:
                await pool.execute("INSERT INTO metric_goals (user_id, kind, target) VALUES ($1,$2,$3)"
                                   " ON CONFLICT (user_id, kind) DO UPDATE SET target=$3", user.id, k, val)
    return await full(await users.get(user.id))


async def topic_on(user_id: int, topic: str | None) -> bool:
    if not topic or topic not in TOPICS:
        return True
    return (await get(user_id))["notify"].get(topic, True)
