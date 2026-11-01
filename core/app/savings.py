"""Цели накоплений: отпуск, ремонт, крупная покупка.

Отложенные деньги — не трата: они хранятся отдельно и не влияют на расходы и лимиты.
Кнопки: v:<goal_id>:add:<сумма> | v:<goal_id>:close | v:0:list
"""
from datetime import date, timedelta
from decimal import Decimal

from . import db, scope
from .finance import money
from .schemas import Button, Reply
from .users import User
from .util import esc, num, parse_date, today


async def goals(family_id: int, include_closed: bool = False) -> list[dict]:
    rows = await db.get_pool().fetch(
        "SELECT g.*, coalesce((SELECT sum(m.amount) FROM savings_moves m WHERE m.goal_id=g.id AND m.deleted_at IS NULL), 0) AS saved,"
        " coalesce((SELECT sum(m.amount) FROM savings_moves m WHERE m.goal_id=g.id AND m.deleted_at IS NULL"
        "   AND m.created_at > now() - interval '7 days'), 0) AS week"
        f" FROM savings_goals g WHERE g.family_id=$1 AND g.deleted_at IS NULL AND {scope.cond('savings', 'g.user_id')}"
        + ("" if include_closed else " AND g.closed_at IS NULL") + " ORDER BY g.closed_at NULLS FIRST, g.created_at", family_id)
    return [dict(r) for r in rows]


async def find(family_id: int, name: str | None) -> dict | None:
    gs = await goals(family_id)
    if not gs:
        return None
    n = (name or "").lower().replace("ё", "е").strip()
    if not n:
        return gs[0] if len(gs) == 1 else None
    for g in gs:
        if g["title"].lower().replace("ё", "е") == n:
            return g
    stems = [w[:max(4, len(w) - 2)] for w in n.split() if len(w) >= 3]
    return next((g for g in gs if any(s in g["title"].lower().replace("ё", "е") for s in stems)), None)


def per_month(g: dict, d: date | None = None) -> Decimal | None:
    """Сколько откладывать в месяц, чтобы успеть к сроку."""
    if not g["deadline"]:
        return None
    d = d or today()
    left = Decimal(str(g["target"])) - Decimal(str(g["saved"]))
    if left <= 0:
        return Decimal(0)
    months = max(1, (g["deadline"].year - d.year) * 12 + g["deadline"].month - d.month)
    return (left / months).quantize(Decimal("1"))


def bar(p: float, width: int = 10) -> str:
    full = max(0, min(width, round(p * width)))
    return "▓" * full + "░" * (width - full)


def line(g: dict) -> str:
    saved, target = Decimal(str(g["saved"])), Decimal(str(g["target"]))
    p = float(saved / target) if target else 0
    s = f"🎯 <b>{esc(g['title'])}</b>: {money(saved)} из {money(target)} ({round(p * 100)}%)\n{bar(p)}"
    if g["deadline"]:
        pm = per_month(g)
        s += f" · до {g['deadline']:%d.%m.%Y}" + (f" · ≈ {money(pm)} в месяц" if pm else " · ✅ собрано")
    if g.get("week"):
        s += f"\nЗа неделю: {'+' if g['week'] > 0 else ''}{money(g['week'])}"
    return s


async def create(user: User, data: dict) -> Reply:
    title = str(data.get("title") or "").strip()[:100]
    target = num(data.get("target"))
    if not title or not target or target <= 0:
        return Reply(text="Укажите цель и сумму: <code>копим на отпуск 150000 к июлю</code>")
    gid = await db.get_pool().fetchval(
        "INSERT INTO savings_goals (family_id, user_id, title, target, deadline) VALUES ($1,$2,$3,$4,$5) RETURNING id",
        user.family_id, user.id, title, target, parse_date(data.get("deadline")))
    start = num(data.get("start_amount"))
    if start:
        await move(user, gid, start)
    g = next(x for x in await goals(user.family_id) if x["id"] == gid)
    return Reply(text="🎯 Новая цель накоплений\n" + line(g) + f"\n\nОтложить: <code>отложил на {esc(title.lower())} 5000</code>",
                 buttons=_quick(gid))


def _quick(gid: int) -> list[list[Button]]:
    return [[Button(text=f"➕ {money(a)}", action=f"v:{gid}:add:{a}") for a in (1000, 5000, 10000)],
            [Button(text="🎯 Все цели", action="v:0:list")]]


async def move(user: User, goal_id: int, amount: float, note: str | None = None) -> int | None:
    async with db.get_pool().acquire() as conn, conn.transaction():
        ok = await conn.fetchval("SELECT id FROM savings_goals WHERE id=$1 AND family_id=$2 AND deleted_at IS NULL",
                                 goal_id, user.family_id)
        if not ok or not amount:
            return None
        mid = await conn.fetchval("INSERT INTO savings_moves (goal_id, user_id, amount, note) VALUES ($1,$2,$3,$4) RETURNING id",
                                  goal_id, user.id, amount, note)
        await db.emit(conn, "savings.moved", {"goal_id": goal_id, "amount": amount, "by": user.name})
    return mid


async def move_by_name(user: User, name: str | None, amount) -> Reply:
    a = num(amount)
    if not a:
        return Reply(text="Укажите сумму: <code>отложил на отпуск 5000</code>")
    g = await find(user.family_id, name)
    if not g:
        gs = await goals(user.family_id)
        if not gs:
            return Reply(text="Целей пока нет. Создайте: <code>копим на отпуск 150000 к июлю</code>")
        return Reply(text=f"На какую цель? Есть: " + ", ".join(esc(x["title"]) for x in gs)
                     + f".\nНапишите: <code>отложил на {esc(gs[0]['title'].lower())} {int(a)}</code>")
    await move(user, g["id"], a)
    g = await find(user.family_id, g["title"])
    head = f"{'💰 Отложено' if a > 0 else '💸 Снято'}: {money(abs(a))}"
    done = Decimal(str(g["saved"])) >= Decimal(str(g["target"]))
    return Reply(text=head + "\n" + line(g) + ("\n\n🎉 Цель достигнута!" if done else ""), buttons=_quick(g["id"]))


async def list_reply(user: User, head: str = "") -> Reply:
    gs = await goals(user.family_id)
    if not gs:
        return Reply(text=(head + "\n\n" if head else "") + "🎯 Целей накоплений нет.\n"
                     "Создайте: <code>копим на отпуск 150000 к июлю</code>, <code>цель ремонт 300 тысяч</code>")
    total = sum(Decimal(str(g["saved"])) for g in gs)
    text = (head + "\n\n" if head else "") + "🎯 <b>Накопления</b>\n\n" + "\n\n".join(line(g) for g in gs) \
        + f"\n\nВсего отложено: <b>{money(total)}</b>"
    buttons = [[Button(text=f"➕ 5 000 · {g['title'][:20]}", action=f"v:{g['id']}:add:5000")] for g in gs[:6]]
    return Reply(text=text, buttons=buttons)


async def handle_action(user: User, parts: list[str]) -> Reply:
    if parts[2] == "list":
        return await list_reply(user)
    gid = int(parts[1])
    if parts[2] == "add" and len(parts) > 3:
        g = next((x for x in await goals(user.family_id) if x["id"] == gid), None)
        if not g:
            return Reply(text="Цель не найдена.")
        return await move_by_name(user, g["title"], parts[3])
    if parts[2] == "close":
        await db.get_pool().execute("UPDATE savings_goals SET closed_at=now() WHERE id=$1 AND family_id=$2", gid, user.family_id)
        return await list_reply(user, "✅ Цель закрыта.")
    return Reply(text="Неизвестное действие.")


async def digest_line(family_id: int) -> str:
    gs = await goals(family_id)
    if not gs:
        return ""
    week = sum(Decimal(str(g["week"])) for g in gs)
    parts = [f"{esc(g['title'])} {round(float(Decimal(str(g['saved'])) / Decimal(str(g['target'])) * 100))}%" for g in gs[:4]]
    return f"🎯 Накопления: {', '.join(parts)}" + (f" · за неделю +{money(week)}" if week > 0 else " · за неделю ничего не отложено")
