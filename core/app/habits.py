"""Привычки и показатели: вода, шаги, вес, сон. Цели и динамика."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from . import db
from .config import get_settings
from .schemas import Button, Reply
from .users import User
from .util import esc, today

KINDS = {
    #         эмодзи, название, единица, цель по умолчанию, как считать день
    "water":  ("💧", "Вода", "мл", Decimal(2000), "sum"),
    "steps":  ("👟", "Шаги", "", Decimal(8000), "max"),
    "weight": ("⚖️", "Вес", "кг", None, "last"),
    "sleep":  ("😴", "Сон", "ч", Decimal(8), "sum"),
    "pulse":  ("❤️", "Пульс", "уд/мин", None, "last"),   # 1.9: средний за день, из Health Connect
}


def fmt(kind: str, v) -> str:
    if v is None:
        return "—"
    v = Decimal(str(v))
    unit = KINDS[kind][2]
    if kind == "weight":
        s = f"{v:.1f}".replace(".", ",")
    elif kind == "sleep":
        s = f"{v:.1f}".rstrip("0").rstrip(".").replace(".", ",")
    else:
        s = f"{int(v):,}".replace(",", " ")
    return f"{s} {unit}".strip()


def _bounds(d: date) -> tuple[datetime, datetime]:
    z = get_settings().zone
    a = datetime.combine(d, time.min, tzinfo=z)
    return a, a + timedelta(days=1)


async def day_value(user_id: int, kind: str, d: date):
    a, b = _bounds(d)
    agg = KINDS[kind][4]
    pool = db.get_pool()
    base = " FROM metrics WHERE user_id=$1 AND kind=$2 AND deleted_at IS NULL AND measured_at >= $3 AND measured_at < $4"
    if agg == "last":
        return await pool.fetchval("SELECT value" + base + " ORDER BY measured_at DESC LIMIT 1", user_id, kind, a, b)
    return await pool.fetchval(f"SELECT {agg}(value)" + base, user_id, kind, a, b)


async def last_weight(user_id: int, before: datetime | None = None):
    return await db.get_pool().fetchval(
        "SELECT value FROM metrics WHERE user_id=$1 AND kind='weight' AND deleted_at IS NULL"
        " AND ($2::timestamptz IS NULL OR measured_at < $2) ORDER BY measured_at DESC LIMIT 1", user_id, before)


async def weight_week_ago(user_id: int):
    """Вес для сравнения: самая ранняя запись за последние 7 дней (если записей больше одной)."""
    rows = await db.get_pool().fetch(
        "SELECT value FROM metrics WHERE user_id=$1 AND kind='weight' AND deleted_at IS NULL"
        " AND measured_at >= now() - interval '7 days' ORDER BY measured_at", user_id)
    return rows[0]["value"] if len(rows) > 1 else await last_weight(user_id, _bounds(today() - timedelta(days=7))[0])


async def goals(user_id: int) -> dict:
    rows = await db.get_pool().fetch("SELECT kind, target FROM metric_goals WHERE user_id=$1", user_id)
    g = {k: v[3] for k, v in KINDS.items()}
    g.update({r["kind"]: r["target"] for r in rows})
    return g


def _bar(v, goal) -> str:
    if not goal:
        return ""
    pct = int(Decimal(str(v or 0)) * 100 / goal)
    return f"{'▰' * min(pct // 10, 10)}{'▱' * (10 - min(pct // 10, 10))} {pct}%"


async def save(user: User, items: list[dict], source: str) -> Reply:
    pool = db.get_pool()
    ids = []
    async with pool.acquire() as conn, conn.transaction():
        for it in items:
            kind = it.get("kind")
            try:
                value = Decimal(str(it.get("value")))
            except Exception:
                continue
            if kind not in KINDS or value <= 0 or value > 200000:
                continue
            mid = await conn.fetchval("INSERT INTO metrics (user_id, kind, value, source) VALUES ($1,$2,$3,$4) RETURNING id",
                                      user.id, kind, value, source)
            ids.append((mid, kind, value))
            await db.emit(conn, "metric.created", {"user_id": user.id, "kind": kind, "value": value})
    if not ids:
        return Reply(text="Не понял показатель. Примеры: <code>выпил 2 стакана воды</code>, <code>вес 82.5</code>, "
                          "<code>прошёл 9000 шагов</code>, <code>спал 7 часов</code>")
    lines = []
    for mid, kind, value in ids:
        e, name, *_ = KINDS[kind]
        lines.append(f"{e} {name}: {('+' if kind == 'water' else '')}{fmt(kind, value)}")
    text = "💾 Записал: " + ", ".join(lines) + "\n\n" + await today_text(user)
    buttons = [[Button(text="💧 +250 мл", action="h:0:w:250"), Button(text="💧 +500 мл", action="h:0:w:500")],
               [Button(text="❌ Отменить", action=f"h:{ids[-1][0]}:del")]]
    return Reply(text=text, buttons=buttons)


async def today_text(user: User) -> str:
    d = today()
    g = await goals(user.id)
    text = f"📈 <b>Привычки за сегодня</b>\n"
    for kind, (e, name, _unit, _g, _agg) in KINDS.items():
        v = await day_value(user.id, kind, d)
        if kind == "weight":
            w = await last_weight(user.id)
            if w is None:
                text += f"{e} {name}: не записан\n"
                continue
            week = await weight_week_ago(user.id)
            delta = ""
            if week is not None and week != w:
                dv = w - week
                delta = f" ({'+' if dv > 0 else '−'}{abs(dv):.1f} за неделю)".replace(".", ",")
            goal = f", цель {fmt(kind, g['weight'])}" if g.get("weight") else ""
            text += f"{e} {name}: {fmt(kind, w)}{delta}{goal}\n"
            continue
        if kind == "pulse":
            if v:
                text += f"{e} {name}: {fmt(kind, v)} (в среднем за день, с телефона)\n"
            continue
        goal = g.get(kind)
        text += f"{e} {name}: {fmt(kind, v or 0)}" + (f" из {fmt(kind, goal)}  {_bar(v, goal)}" if goal else "") + "\n"
    return text


async def show(user: User) -> Reply:
    text = await today_text(user) + "\n" + await week_text(user)
    text += ("\nЗаписать: <code>выпил стакан воды</code>, <code>вес 82.5</code>, <code>9000 шагов</code>, "
             "<code>спал 7 часов</code>\nЦель: <code>цель вода 2 литра</code>, <code>цель вес 78</code>")
    return Reply(text=text, buttons=[[Button(text="💧 +250 мл", action="h:0:w:250"),
                                      Button(text="💧 +500 мл", action="h:0:w:500")]])


async def week_series(user_id: int, kind: str, days: int = 7) -> list[tuple[date, object]]:
    d = today()
    return [(d - timedelta(days=i), await day_value(user_id, kind, d - timedelta(days=i))) for i in range(days - 1, -1, -1)]


async def week_text(user: User) -> str:
    g = await goals(user.id)
    text = "<b>За 7 дней</b>\n"
    any_data = False
    for kind in ("water", "steps", "sleep"):
        vals = [v for _, v in await week_series(user.id, kind) if v]
        if not vals:
            continue
        any_data = True
        avg = sum(vals) / len(vals)
        hit = sum(1 for v in vals if g.get(kind) and v >= g[kind])
        e, name, *_ = KINDS[kind]
        text += f"{e} {name}: в среднем {fmt(kind, avg)}" + (f", цель выполнена {hit} из 7 дн." if g.get(kind) else "") + "\n"
    return text if any_data else ""


async def week_stats(user: User) -> dict:
    """Для недельного разбора и утренней сводки."""
    g = await goals(user.id)
    out = {}
    for kind in ("water", "steps", "sleep"):
        vals = [float(v) for _, v in await week_series(user.id, kind) if v]
        if vals:
            out[kind] = {"avg": round(sum(vals) / len(vals), 1), "days": len(vals),
                         "goal": float(g[kind]) if g.get(kind) else None}
    w = await last_weight(user.id)
    if w is not None:
        week = await weight_week_ago(user.id)
        out["weight"] = {"now": float(w), "week_ago": float(week) if week is not None else None,
                         "goal": float(g["weight"]) if g.get("weight") else None}
    return out


async def set_goal(user: User, kind: str, target) -> Reply:
    if kind not in KINDS:
        return Reply(text="Цель можно поставить для: вода, шаги, вес, сон.")
    try:
        t = Decimal(str(target))
    except Exception:
        t = Decimal(0)
    pool = db.get_pool()
    e, name, *_ = KINDS[kind]
    if t <= 0:
        await pool.execute("DELETE FROM metric_goals WHERE user_id=$1 AND kind=$2", user.id, kind)
        return Reply(text=f"{e} Цель «{name}» убрана.")
    await pool.execute("INSERT INTO metric_goals (user_id, kind, target) VALUES ($1,$2,$3)"
                       " ON CONFLICT (user_id, kind) DO UPDATE SET target=$3", user.id, kind, t)
    return Reply(text=f"🎯 {e} Цель «{name}»: {fmt(kind, t)}\n\n" + await today_text(user))


async def handle_action(user: User, parts: list[str]) -> Reply:
    """h:0:w:<мл> | h:<id>:del"""
    if parts[2] == "w" and len(parts) > 3:
        return await save(user, [{"kind": "water", "value": int(parts[3])}], source="button")
    if parts[2] == "del":
        n = await db.get_pool().execute("UPDATE metrics SET deleted_at=now() WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL",
                                        int(parts[1]), user.id)
        return Reply(text=("🗑 Отменено.\n\n" if n.endswith("1") else "Уже отменено.\n\n") + await today_text(user))
    return Reply(text="Неизвестное действие.")
