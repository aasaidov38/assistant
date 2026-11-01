"""Месячные лимиты по категориям и предупреждения при приближении к ним."""
from datetime import date
from decimal import Decimal

from . import db, finance, notify, scope, users
from .schemas import Reply
from .util import esc, today

LEVELS = (80, 100)     # при каких процентах предупреждать


def _month(d: date) -> tuple[date, date]:
    start = d.replace(day=1)
    return start, date(start.year + (start.month == 12), start.month % 12 + 1, 1)


async def set_limit(user: users.User, category: str, limit) -> Reply:
    cat_id = await finance.category_id("expense", category)
    cat = next(c for c in await finance.categories("expense") if c["id"] == cat_id)
    amount = finance.num(limit)
    pool = db.get_pool()
    if amount <= 0:
        await pool.execute("DELETE FROM budgets WHERE family_id=$1 AND category_id=$2", user.family_id, cat_id)
        return Reply(text=f"🗑 Лимит на «{cat['emoji']} {esc(cat['name'])}» убран.")
    await pool.execute(
        "INSERT INTO budgets (family_id, category_id, monthly_limit) VALUES ($1,$2,$3)"
        " ON CONFLICT (family_id, category_id) DO UPDATE SET monthly_limit=$3, updated_at=now()",
        user.family_id, cat_id, amount)
    # Предупреждения этого месяца пересчитываются под новый лимит
    await pool.execute("DELETE FROM budget_alerts WHERE family_id=$1 AND category_id=$2 AND month=$3",
                       user.family_id, cat_id, _month(today())[0])
    spent = await _spent(user.family_id, cat_id, today())
    return Reply(text=f"🎯 Лимит на «{cat['emoji']} {esc(cat['name'])}»: <b>{finance.money(amount)}</b> в месяц.\n"
                      f"Уже потрачено в этом месяце: {finance.money(spent)} ({_pct(spent, amount)}%)\n\n"
                      + await status_text(user))


def _pct(spent, limit) -> int:
    return int(spent * 100 / limit) if limit else 0


def _bar(pct: int) -> str:
    filled = min(pct // 10, 10)
    return ("🟥" if pct >= 100 else "🟨" if pct >= 80 else "🟩") + " " + "▰" * filled + "▱" * (10 - filled)


async def _spent(family_id: int, cat_id: int, d: date) -> Decimal:
    a, b = _month(d)
    return await db.get_pool().fetchval(
        "SELECT coalesce(sum(amount),0) FROM transactions WHERE family_id=$1 AND category_id=$2"
        " AND kind='expense' AND deleted_at IS NULL AND occurred_on >= $3 AND occurred_on < $4"
        f" AND {scope.cond('finance', 'user_id')}",
        family_id, cat_id, a, b)


async def status_rows(family_id: int, d: date | None = None) -> list[dict]:
    a, b = _month(d or today())
    rows = await db.get_pool().fetch(
        "SELECT c.id, c.name, c.emoji, b.monthly_limit AS lim,"
        " coalesce((SELECT sum(t.amount) FROM transactions t WHERE t.family_id=b.family_id"
        "  AND t.category_id=c.id AND t.kind='expense' AND t.deleted_at IS NULL"
        "  AND t.occurred_on >= $2 AND t.occurred_on < $3"
        f"  AND {scope.cond('finance', 't.user_id')}), 0) AS spent"
        " FROM budgets b JOIN categories c ON c.id=b.category_id WHERE b.family_id=$1"
        " ORDER BY c.sort, c.name", family_id, a, b)
    return [dict(r) for r in rows]


async def status_text(user: users.User) -> str:
    rows = await status_rows(user.family_id)
    if not rows:
        return ("🎯 Лимитов пока нет.\nПоставить: <code>лимит на продукты 30000</code>\n"
                "Убрать: <code>убери лимит на продукты</code>")
    d = today()
    a, b = _month(d)
    days_total = (b - a).days
    text = f"🎯 <b>Бюджет на месяц</b> (прошло {d.day} из {days_total} дн.)\n"
    for r in rows:
        pct = _pct(r["spent"], r["lim"])
        left = r["lim"] - r["spent"]
        tail = f"осталось {finance.money(left)}" if left >= 0 else f"перерасход {finance.money(-left)}"
        text += (f"\n{r['emoji']} <b>{esc(r['name'])}</b>: {finance.money(r['spent'])} из {finance.money(r['lim'])}"
                 f"\n{_bar(pct)} {pct}% · {tail}\n")
    return text


async def check_after_save(user: users.User, rows: list[dict]) -> str:
    """Проверяет лимиты по категориям только что сохранённых трат.

    Возвращает текст предупреждения для автора; остальным членам семьи отправляет уведомление."""
    cats = {r["category_id"] for r in rows if r.get("kind") == "expense"}
    if not cats:
        return ""
    lines = []
    month = _month(today())[0]
    pool = db.get_pool()
    for r in await status_rows(user.family_id):
        if r["id"] not in cats:
            continue
        pct = _pct(r["spent"], r["lim"])
        reached = [lv for lv in LEVELS if pct >= lv]
        if not reached:
            continue
        level = max(reached)
        new = await pool.fetchval(
            "INSERT INTO budget_alerts (family_id, category_id, month, level) VALUES ($1,$2,$3,$4)"
            " ON CONFLICT DO NOTHING RETURNING level", user.family_id, r["id"], month, level)
        if not new:
            continue
        # Отметим и уровень ниже, чтобы после 100% не пришло запоздалое «80%»
        for lv in LEVELS:
            if lv < level:
                await pool.execute(
                    "INSERT INTO budget_alerts (family_id, category_id, month, level) VALUES ($1,$2,$3,$4)"
                    " ON CONFLICT DO NOTHING", user.family_id, r["id"], month, lv)
        if level >= 100:
            lines.append(f"🟥 Лимит на «{r['emoji']} {esc(r['name'])}» превышен: "
                         f"{finance.money(r['spent'])} из {finance.money(r['lim'])} ({pct}%)")
        else:
            lines.append(f"🟨 На «{r['emoji']} {esc(r['name'])}» потрачено {pct}% лимита: "
                         f"{finance.money(r['spent'])} из {finance.money(r['lim'])}, "
                         f"осталось {finance.money(r['lim'] - r['spent'])}")
    if not lines:
        return ""
    text = "\n".join(lines)
    if not await scope.personal(user.family_id, "finance"):
        for m in await users.family_members(user.family_id):
            if m.id != user.id:
                await notify.send(m.id, Reply(text=f"💬 Новая трата ({esc(user.name)}).\n{text}"), topic="budget")
    async with pool.acquire() as conn:
        await db.emit(conn, "budget.alert", {"family_id": user.family_id, "text": text})
    return text
