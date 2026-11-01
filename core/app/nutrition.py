"""Дневник питания и КБЖУ."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from . import db
from .config import get_settings
from .schemas import Button, Reply
from .users import User
from .util import esc, now, today

MEAL_NAMES = {"breakfast": "🍳 Завтрак", "lunch": "🍲 Обед", "dinner": "🍽 Ужин", "snack": "🍎 Перекус"}


def _n(v) -> Decimal:
    try:
        return Decimal(str(v)).quantize(Decimal("0.1"))
    except Exception:
        return Decimal(0)


def _guess_meal_type() -> str:
    h = now().hour
    return "breakfast" if 5 <= h < 11 else "lunch" if 11 <= h < 16 else "dinner" if 17 <= h < 22 else "snack"


async def save(user: User, m: dict, source: str) -> dict:
    meal_type = m.get("meal_type") if m.get("meal_type") in MEAL_NAMES else _guess_meal_type()
    async with db.get_pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "INSERT INTO meals (user_id, eaten_at, meal_type, title, grams, kcal, protein, fat, carbs, items, source)"
            " VALUES ($1, now(), $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING *",
            user.id, meal_type, (m.get("title") or "Без названия")[:200],
            _n(m["grams"]) if m.get("grams") else None,
            _n(m.get("kcal")), _n(m.get("protein")), _n(m.get("fat")), _n(m.get("carbs")),
            m.get("items"), source)
        meal = dict(row)
        await db.emit(conn, "meal.created", meal)
    return meal


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    z = get_settings().zone
    start = datetime.combine(day, time.min, tzinfo=z)
    return start, start + timedelta(days=1)


async def day_totals(user: User, day: date) -> dict:
    a, b = _day_bounds(day)
    row = await db.get_pool().fetchrow(
        "SELECT count(*) AS n, coalesce(sum(kcal),0) AS kcal, coalesce(sum(protein),0) AS protein,"
        " coalesce(sum(fat),0) AS fat, coalesce(sum(carbs),0) AS carbs FROM meals"
        " WHERE user_id=$1 AND deleted_at IS NULL AND eaten_at >= $2 AND eaten_at < $3", user.id, a, b)
    return dict(row)


def goal_for(user: User) -> int:
    return user.kcal_goal or get_settings().default_kcal_goal


def _bar(value, goal) -> str:
    pct = min(int(value * 100 / goal), 999) if goal else 0
    filled = min(pct // 10, 10)
    return f"{'▰' * filled}{'▱' * (10 - filled)} {pct}%"


async def render_saved(user: User, meal: dict, header: str = "") -> Reply:
    t = await day_totals(user, today())
    goal = goal_for(user)
    text = (header + "\n" if header else "")
    text += f"{MEAL_NAMES.get(meal['meal_type'], '🍽')}: <b>{esc(meal['title'])}</b>"
    if meal.get("grams"):
        text += f" (~{meal['grams']:.0f} г)"
    text += (f"\n🔥 {meal['kcal']:.0f} ккал · Б {meal['protein']:.0f} · Ж {meal['fat']:.0f} · У {meal['carbs']:.0f}\n")
    items = meal.get("items") or []
    if len(items) > 1:
        text += "".join(f"  • {esc(str(i.get('name')))}: {float(i.get('kcal') or 0):.0f} ккал\n" for i in items[:8])
    text += (f"\n<b>За сегодня</b>: {t['kcal']:.0f} из {goal} ккал\n{_bar(t['kcal'], goal)}\n"
             f"Б {t['protein']:.0f} · Ж {t['fat']:.0f} · У {t['carbs']:.0f}")
    mid = meal["id"]
    buttons = [[Button(text="½ порции", action=f"m:{mid}:x:0.5"),
                Button(text="×1.5", action=f"m:{mid}:x:1.5"),
                Button(text="×2", action=f"m:{mid}:x:2")],
               [Button(text="❌ Отменить", action=f"m:{mid}:del")]]
    return Reply(text=text, buttons=buttons)


async def handle_action(user: User, parts: list[str]) -> Reply:
    """m:<id>:del | m:<id>:x:<коэффициент>"""
    meal_id = int(parts[1])
    async with db.get_pool().acquire() as conn, conn.transaction():
        meal = await conn.fetchrow("SELECT * FROM meals WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL",
                                   meal_id, user.id)
        if not meal:
            return Reply(text="Запись не найдена или уже удалена.")
        if parts[2] == "del":
            await conn.execute("UPDATE meals SET deleted_at=now() WHERE id=$1", meal_id)
            await db.emit(conn, "meal.deleted", {"id": meal_id})
            return Reply(text=f"🗑 Удалено: {esc(meal['title'])}")
        if parts[2] == "x" and parts[3] in ("0.5", "1.5", "2"):
            k = Decimal(parts[3])
            items = [{**i, **{f: round(float(i[f]) * float(k), 1) for f in
                              ("grams", "kcal", "protein", "fat", "carbs") if isinstance(i.get(f), (int, float))}}
                     for i in (meal["items"] or []) if isinstance(i, dict)]
            row = await conn.fetchrow(
                "UPDATE meals SET grams=grams*$2, kcal=kcal*$2, protein=protein*$2, fat=fat*$2, carbs=carbs*$2,"
                " items=$3 WHERE id=$1 RETURNING *", meal_id, k, items or None)
            meal = dict(row)
            await db.emit(conn, "meal.updated", meal)
    return await render_saved(user, meal, header=f"✏️ Порция ×{parts[3]}")


async def day_report(user: User, day: date) -> str:
    a, b = _day_bounds(day)
    rows = await db.get_pool().fetch(
        "SELECT meal_type, title, kcal, protein, fat, carbs FROM meals"
        " WHERE user_id=$1 AND deleted_at IS NULL AND eaten_at >= $2 AND eaten_at < $3 ORDER BY eaten_at",
        user.id, a, b)
    goal = goal_for(user)
    if not rows:
        return f"🍽 Питание: пока ничего не записано (цель {goal} ккал).\nПришлите фото еды или напишите, что съели."
    t = await day_totals(user, day)
    text = "🍽 <b>Питание</b>\n"
    for r in rows:
        text += f"{MEAL_NAMES.get(r['meal_type'], '🍽').split()[0]} {esc(r['title'])}: {r['kcal']:.0f} ккал\n"
    left = goal - t["kcal"]
    text += (f"\nВсего: <b>{t['kcal']:.0f}</b> из {goal} ккал\n{_bar(t['kcal'], goal)}\n"
             f"Б {t['protein']:.0f} г · Ж {t['fat']:.0f} г · У {t['carbs']:.0f} г\n")
    text += f"Осталось: {left:.0f} ккал" if left >= 0 else f"Сверх цели: {-left:.0f} ккал"
    return text


async def set_goal(user: User, kcal: int) -> None:
    await db.get_pool().execute("UPDATE users SET kcal_goal=$2 WHERE id=$1", user.id, kcal)
