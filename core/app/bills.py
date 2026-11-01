"""Регулярные платежи и подписки: кредиты, ЖКХ, связь, подписки.

Бот напоминает за remind_days дней до срока, в день срока и каждый день до недели просрочки.
«✅ Оплачено» сразу записывает трату и переносит срок на следующий период.
Кнопки: b:<id>:pay | b:<id>:other | b:<id>:skip | b:<id>:del | b:<id>:delok | b:0:list
"""
from datetime import date, timedelta
from decimal import Decimal

from . import db, finance, notify, scope
from .config import get_settings
from .schemas import Button, Reply
from .users import User
from .util import add_months, esc, now, num, parse_date, today


def first_due(day: int, period: int = 1, start: date | None = None) -> date:
    """Ближайшая дата с этим числом месяца, начиная с сегодня."""
    t = start or today()
    d = add_months(t.replace(day=1), 0, day)
    return d if d >= t else add_months(t.replace(day=1), 1, day)


async def find(family_id: int, title: str | None) -> dict | None:
    """Ищет платёж по названию: точное, затем по словам."""
    rows = await db.get_pool().fetch(
        f"SELECT * FROM bills WHERE family_id=$1 AND deleted_at IS NULL AND {scope.cond('bills', 'user_id')} ORDER BY next_due", family_id)
    t = (title or "").lower().replace("ё", "е").strip()
    if not t:
        return None
    for r in rows:
        if r["title"].lower().replace("ё", "е") == t:
            return dict(r)
    words = [w[:max(4, len(w) - 2)] for w in t.split() if len(w) >= 3]
    for r in rows:
        name = r["title"].lower().replace("ё", "е")
        if words and all(w in name for w in words):
            return dict(r)
    for r in rows:
        name = r["title"].lower().replace("ё", "е")
        if any(w in name for w in words):
            return dict(r)
    return None


async def create(user: User, b: dict) -> Reply:
    title = str(b.get("title") or "").strip()[:100]
    day = int(num(b.get("day")) or 0)
    nd = parse_date(b.get("next_date"))          # «раз в год 15 марта» → конкретная дата
    if nd:
        day = nd.day
    if not title:
        return Reply(text="Не понял название платежа. Пример: <code>кредит Сбер 15000 каждое 10 число</code>")
    if not 1 <= day <= 31:
        return Reply(text=f"Какого числа платить «{esc(title)}»? Напишите, например: "
                          f"<code>{esc(title)} {int(num(b.get('amount')) or 0) or ''} каждое 10 число</code>".replace("  ", " "))
    period = int(num(b.get("period_months")) or 1)
    period = min(max(period, 1), 24)
    if nd:
        while nd < today():
            nd = add_months(nd, period, day)
    remind = int(num(b.get("remind_days")) if b.get("remind_days") is not None else 3)
    amount = num(b.get("amount"))
    cat = await finance.category_id("expense", b.get("category"))
    existing = await find(user.family_id, title)
    pool = db.get_pool()
    if existing and existing["title"].lower() == title.lower():
        await pool.execute(
            "UPDATE bills SET amount=coalesce($2, amount), category_id=$3, due_day=$4, period_months=$5,"
            " remind_days=$6, next_due=$7 WHERE id=$1", existing["id"], amount, cat, day, period, max(0, min(remind, 30)),
            nd or first_due(day, period, max(today(), existing["next_due"])))
        bill_id, head = existing["id"], "✏️ Обновил платёж"
    else:
        bill_id = await pool.fetchval(
            "INSERT INTO bills (family_id, user_id, title, amount, category_id, due_day, period_months, next_due, remind_days)"
            " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id", user.family_id, user.id, title, amount, cat, day,
            period, nd or first_due(day, period), max(0, min(remind, 30)))
        head = "💳 Добавил регулярный платёж"
    bill = await get(user.family_id, bill_id)
    return Reply(text=f"{head}\n{line(bill)}\n\nНапомню за {bill['remind_days']} дн. и в день оплаты. "
                      "Все платежи: /bills", buttons=[[Button(text="🗑 Удалить", action=f"b:{bill_id}:del")]])


async def get(family_id: int, bill_id: int) -> dict | None:
    r = await db.get_pool().fetchrow(
        "SELECT b.*, c.name AS category, c.emoji FROM bills b LEFT JOIN categories c ON c.id=b.category_id"
        " WHERE b.id=$1 AND b.family_id=$2 AND b.deleted_at IS NULL"
        f" AND {scope.cond('bills', 'b.user_id')}", bill_id, family_id)
    return dict(r) if r else None


async def all_bills(family_id: int) -> list[dict]:
    rows = await db.get_pool().fetch(
        "SELECT b.*, c.name AS category, c.emoji,"
        " (SELECT max(p.created_at) FROM bill_payments p WHERE p.bill_id=b.id AND p.deleted_at IS NULL) AS last_paid_at"
        " FROM bills b LEFT JOIN categories c ON c.id=b.category_id"
        f" WHERE b.family_id=$1 AND b.deleted_at IS NULL AND {scope.cond('bills', 'b.user_id')} ORDER BY b.next_due, b.title", family_id)
    return [dict(r) for r in rows]


def _period(b: dict) -> str:
    p = b["period_months"]
    return "ежемесячно" if p == 1 else "раз в квартал" if p == 3 else "раз в полгода" if p == 6 \
        else "раз в год" if p == 12 else f"раз в {p} мес."


def status(b: dict, d: date | None = None) -> tuple[str, int]:
    """(значок, дней до срока; минус = просрочка)."""
    d = d or today()
    left = (b["next_due"] - d).days
    if left < 0:
        return "🔴", left
    if left <= b["remind_days"]:
        return "🟡", left
    return "⚪", left


def _left_text(left: int) -> str:
    if left < 0:
        return f"просрочен на {-left} дн."
    if left == 0:
        return "сегодня"
    if left == 1:
        return "завтра"
    return f"через {left} дн."


def line(b: dict) -> str:
    icon, left = status(b)
    amount = finance.money(b["amount"]) if b["amount"] else "сумма разная"
    return (f"{icon} <b>{esc(b['title'])}</b> — {amount}, до {b['next_due']:%d.%m} ({_left_text(left)})"
            f" · {_period(b)}, {b['due_day']}-го")


def month_totals(rows: list[dict], d: date | None = None) -> tuple[Decimal, Decimal]:
    """(осталось оплатить в этом месяце, уже оплачено в этом месяце) по известным суммам."""
    d = d or today()
    end = add_months(d.replace(day=1), 1)
    left = sum((r["amount"] or 0 for r in rows if r["next_due"] < end), Decimal(0))
    return Decimal(left), Decimal(0)


async def paid_this_month(family_id: int) -> Decimal:
    d = today().replace(day=1)
    v = await db.get_pool().fetchval(
        "SELECT coalesce(sum(t.amount),0) FROM bill_payments p JOIN bills b ON b.id=p.bill_id"
        " JOIN transactions t ON t.id=p.transaction_id WHERE b.family_id=$1 AND p.deleted_at IS NULL"
        " AND t.deleted_at IS NULL AND t.occurred_on >= $2"
        f" AND {scope.cond('bills', 'b.user_id')}", family_id, d)
    return Decimal(str(v or 0))


async def list_reply(user: User, head: str = "") -> Reply:
    rows = await all_bills(user.family_id)
    if not rows:
        return Reply(text=(head + "\n\n" if head else "") +
                     "💳 Регулярных платежей пока нет.\nДобавьте так: <code>кредит Сбер 15000 каждое 10 число</code>, "
                     "<code>интернет 650 каждое 5 число</code>, <code>КАСКО 42000 раз в год 15 марта</code>")
    left, _ = month_totals(rows)
    paid = await paid_this_month(user.family_id)
    text = (head + "\n\n" if head else "") + "💳 <b>Регулярные платежи</b>\n" + "\n".join(line(r) for r in rows)
    text += f"\n\nВ этом месяце осталось: <b>{finance.money(left)}</b> · уже оплачено: {finance.money(paid)}"
    buttons = []
    for r in rows:
        if status(r)[1] <= max(r["remind_days"], 7):
            buttons.append([Button(text=f"✅ {r['title'][:24]}", action=f"b:{r['id']}:pay")])
    return Reply(text=text, buttons=buttons[:10])


async def pay(user: User, bill_id: int, amount=None, skip: bool = False) -> tuple[dict | None, str]:
    """Отмечает текущий срок оплаченным (или пропущенным) и переносит срок. Возвращает (платёж, текст)."""
    pool = db.get_pool()
    b = await get(user.family_id, bill_id)
    if not b:
        return None, "Платёж не найден."
    amount = num(amount) if amount is not None else (float(b["amount"]) if b["amount"] else None)
    tx_id = None
    if not skip:
        if not amount:
            return None, f"Сколько заплатили за «{esc(b['title'])}»? Напишите: <code>оплатил {esc(b['title'])} 6230</code>"
        rows = await finance.save(user, [{"kind": "expense", "amount": amount, "category": b["category"],
                                          "merchant": b["title"], "note": "регулярный платёж", "date": today().isoformat()}],
                                  source="bill")
        tx_id = rows[0]["id"] if rows else None
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("INSERT INTO bill_payments (bill_id, due_on, transaction_id, skipped, user_id)"
                           " VALUES ($1,$2,$3,$4,$5)", bill_id, b["next_due"], tx_id, skip, user.id)
        nxt = add_months(b["next_due"], b["period_months"], b["due_day"])
        await conn.execute("UPDATE bills SET next_due=$2, last_reminded_on=NULL WHERE id=$1", bill_id, nxt)
        await db.emit(conn, "bill.paid" if not skip else "bill.skipped",
                      {"bill_id": bill_id, "title": b["title"], "amount": amount, "due_on": b["next_due"].isoformat()})
    b["next_due"] = nxt
    if skip:
        return b, f"⏭ «{esc(b['title'])}»: этот срок пропущен. Следующий — {nxt:%d.%m.%Y}."
    return b, (f"✅ Оплачено: {esc(b['title'])} — {finance.money(amount)}\n"
               f"Записал трату. Следующий платёж — {nxt:%d.%m.%Y}.")


async def pay_by_name(user: User, title: str | None, amount) -> Reply:
    b = await find(user.family_id, title)
    if not b:
        return Reply(text=f"Не нашёл регулярный платёж «{esc(title or '')}». Список: /bills")
    done, text = await pay(user, b["id"], amount)
    return Reply(text=text, buttons=[[Button(text="💳 Все платежи", action="b:0:list")]] if done else [])


async def handle_action(user: User, parts: list[str]) -> Reply:
    op = parts[2]
    if op == "list":
        return await list_reply(user)
    bill_id = int(parts[1])
    if op in ("pay", "skip"):
        b, text = await pay(user, bill_id, skip=op == "skip")
        return Reply(text=text, buttons=[[Button(text="💳 Все платежи", action="b:0:list")]])
    if op == "other":
        b = await get(user.family_id, bill_id)
        if not b:
            return Reply(text="Платёж не найден.")
        return Reply(text=f"Напишите, сколько заплатили: <code>оплатил {esc(b['title'])} 6230</code>")
    if op == "del":
        b = await get(user.family_id, bill_id)
        if not b:
            return Reply(text="Платёж не найден.")
        return Reply(text=f"Удалить регулярный платёж «{esc(b['title'])}»? Напоминаний больше не будет. "
                          "Уже записанные траты останутся.",
                     buttons=[[Button(text="🗑 Да, удалить", action=f"b:{bill_id}:delok"),
                               Button(text="Отмена", action="b:0:list")]])
    if op == "delok":
        await delete(user, bill_id)
        return await list_reply(user, "🗑 Платёж удалён.")
    return Reply(text="Неизвестное действие.")


async def delete(user: User, bill_id: int) -> bool:
    n = await db.get_pool().execute("UPDATE bills SET deleted_at=now() WHERE id=$1 AND family_id=$2 AND deleted_at IS NULL",
                                    bill_id, user.family_id)
    return n.endswith("1")


# ---------- Связь с отменой трат ----------

async def on_tx_cancelled(conn, tx_ids: list[int]) -> None:
    """Если отменили трату-оплату, срок платежа возвращается назад."""
    rows = await conn.fetch("SELECT p.id, p.bill_id, p.due_on FROM bill_payments p WHERE p.transaction_id = ANY($1::bigint[])"
                            " AND p.deleted_at IS NULL", tx_ids)
    for r in rows:
        await conn.execute("UPDATE bill_payments SET deleted_at=now() WHERE id=$1", r["id"])
        await conn.execute("UPDATE bills SET next_due=LEAST(next_due, $2) WHERE id=$1", r["bill_id"], r["due_on"])


async def on_tx_restored(conn, tx_ids: list[int]) -> None:
    rows = await conn.fetch(
        "SELECT p.id, p.bill_id, p.due_on, b.period_months, b.due_day, b.next_due FROM bill_payments p"
        " JOIN bills b ON b.id=p.bill_id WHERE p.transaction_id = ANY($1::bigint[]) AND p.deleted_at IS NOT NULL", tx_ids)
    for r in rows:
        await conn.execute("UPDATE bill_payments SET deleted_at=NULL WHERE id=$1", r["id"])
        if r["next_due"] <= r["due_on"]:
            await conn.execute("UPDATE bills SET next_due=$2 WHERE id=$1", r["bill_id"],
                               add_months(r["due_on"], r["period_months"], r["due_day"]))


# ---------- Напоминания ----------

def _need_remind(b: dict, d: date) -> bool:
    start = b["next_due"] - timedelta(days=b["remind_days"])
    last = b["last_reminded_on"]
    if d < start or d > b["next_due"] + timedelta(days=7):
        return False
    if last is None or last < start:
        return True                                  # первое напоминание в этом цикле
    return d >= b["next_due"] and last < d           # в день срока и при просрочке — каждый день


async def send_due() -> None:
    n = now()
    if n.hour < get_settings().daily_reminder_hour:
        return
    d = n.date()
    rows = await db.get_pool().fetch("SELECT * FROM bills WHERE deleted_at IS NULL AND next_due <= $1", d + timedelta(days=30))
    for r in rows:
        b = dict(r)
        if not _need_remind(b, d):
            continue
        ok = await db.get_pool().fetchval(
            "UPDATE bills SET last_reminded_on=$2 WHERE id=$1 AND (last_reminded_on IS NULL OR last_reminded_on < $2)"
            " RETURNING id", b["id"], d)
        if not ok:
            continue
        _, left = status(b, d)
        head = "⚠️ <b>Просрочен платёж</b>" if left < 0 else "💳 <b>Скоро платёж</b>" if left > 0 else "💳 <b>Сегодня платёж</b>"
        amount = finance.money(b["amount"]) if b["amount"] else "сумма не указана"
        btn_pay = Button(text=f"✅ Оплачено {finance.money(b['amount'])}" if b["amount"] else "✅ Оплачено",
                         action=f"b:{b['id']}:pay" if b["amount"] else f"b:{b['id']}:other")
        reply = Reply(text=f"{head}\n{esc(b['title'])} — {amount}, до {b['next_due']:%d.%m} ({_left_text(left)})",
                      buttons=[[btn_pay], [Button(text="✏️ Другая сумма", action=f"b:{b['id']}:other"),
                                           Button(text="⏭ Пропустить срок", action=f"b:{b['id']}:skip")]])
        await notify.remind(b["family_id"], b["user_id"], reply, topic="bills", section="bills")


async def morning_lines(user: User) -> list[str]:
    rows = [r for r in await all_bills(user.family_id) if status(r)[1] <= 3]
    return [f"💳 {esc(r['title'])}: {finance.money(r['amount']) if r['amount'] else '—'}, {_left_text(status(r)[1])}"
            for r in rows[:5]]
