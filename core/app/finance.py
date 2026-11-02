"""Учёт семейных доходов и расходов."""
import logging
import uuid
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from . import db, scope
from .config import get_settings
from .schemas import Button, Reply
from .users import User
from .util import esc, plural, today


def money(v) -> str:
    v = Decimal(str(v))
    s = f"{v:,.2f}".replace(",", " ")
    if s.endswith(".00"):
        s = s[:-3]
    return s.replace(".", ",") + " ₽"


async def categories(kind: str | None = None) -> list[dict]:
    q = "SELECT id, kind, name, emoji FROM categories WHERE is_active"
    args = []
    if kind:
        q += " AND kind=$1"
        args.append(kind)
    rows = await db.get_pool().fetch(q + " ORDER BY kind, sort, name", *args)
    return [dict(r) for r in rows]


async def category_names(kind: str) -> list[str]:
    return [c["name"] for c in await categories(kind)]


async def category_id(kind: str, name: str | None) -> int:
    cats = await categories(kind)
    if name:
        n = name.strip().lower()
        for c in cats:
            if c["name"].lower() == n:
                return c["id"]
        for c in cats:   # частичное совпадение: «продукт» -> «Продукты»
            if n and (n in c["name"].lower() or c["name"].lower() in n):
                return c["id"]
    fallback = "Прочее" if kind == "expense" else "Прочие доходы"
    for c in cats:
        if c["name"] == fallback:
            return c["id"]
    return cats[-1]["id"]


def _to_date(v) -> date:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return today()


async def save(user: User, items: list[dict], source: str, raw: dict | None = None,
               group: bool = False) -> list[dict]:
    """items: [{kind, amount, category, merchant, note, date}] → сохранённые строки."""
    group_id = uuid.uuid4() if group and len(items) > 1 else None
    saved = []
    async with db.get_pool().acquire() as conn, conn.transaction():
        for it in items:
            kind = "income" if it.get("kind") == "income" else "expense"
            try:
                amount = Decimal(str(it.get("amount"))).quantize(Decimal("0.01"), ROUND_HALF_UP)
            except Exception:
                continue
            if amount <= 0:
                continue
            cat_id = await category_id(kind, it.get("category"))
            row = await conn.fetchrow(
                "INSERT INTO transactions (family_id, user_id, kind, amount, currency, category_id,"
                " merchant, note, occurred_on, source, group_id, raw, vehicle_id, trip_id)"
                " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,"
                # поездка: явно указанная, иначе та, в даты которой попала трата (кроме регулярных платежей)
                "  coalesce($14::bigint, CASE WHEN $10 <> 'bill' THEN (SELECT tr.id FROM trips tr WHERE tr.family_id=$1"
                "   AND tr.deleted_at IS NULL AND $9::date BETWEEN tr.start_on AND tr.end_on ORDER BY tr.start_on DESC LIMIT 1) END))"
                " RETURNING id",
                user.family_id, user.id, kind, amount, get_settings().currency, cat_id,
                it.get("merchant"), it.get("note"), _to_date(it.get("date")), source, group_id, raw,
                it.get("vehicle_id"), it.get("trip_id"))
            for i in it.get("_items") or []:
                await conn.execute(
                    "INSERT INTO transaction_items (transaction_id, name, qty, amount, subcategory)"
                    " VALUES ($1,$2,$3,$4,$5)", row["id"], str(i.get("name") or "?")[:200],
                    _qty(i.get("qty")), num(i.get("sum")), (i.get("group") or None))
            saved.append(await _load(conn, row["id"]))
        for t in saved:
            await db.emit(conn, "transaction.created", t)
    if saved and source not in ("bank_push", "bank"):
        from . import bankfeed       # 1.9: ручная запись после автоматической из уведомления банка
        try:
            await bankfeed.absorb_manual(user, saved)
        except Exception:
            logging.getLogger("finance").exception("Сверка с уведомлениями банка")
    return saved


def _qty(v):
    q = num(v)
    return q if 0 < q < 10000 else None


async def _load(conn, tx_id: int) -> dict:
    row = await conn.fetchrow(
        "SELECT t.id, t.family_id, t.user_id, u.name AS user_name, t.kind, t.amount, t.currency,"
        " t.category_id, c.name AS category, c.emoji, t.merchant, t.note, t.occurred_on, t.source,"
        " t.group_id, t.deleted_at, (SELECT count(*) FROM transaction_items i WHERE i.transaction_id=t.id) AS n_items"
        " FROM transactions t JOIN categories c ON c.id=t.category_id JOIN users u ON u.id=t.user_id"
        " WHERE t.id=$1", tx_id)
    return dict(row) if row else {}


def _line(t: dict) -> str:
    sign = "➕" if t["kind"] == "income" else "➖"
    where = f" · {esc(t['merchant'])}" if t.get("merchant") else ""
    note = f" ({esc(t['note'])})" if t.get("note") else ""
    day = "" if t["occurred_on"] == today() else f" · {t['occurred_on']:%d.%m}"
    items = f" · {plural(t['n_items'], 'позиция', 'позиции', 'позиций')}" if t.get("n_items") else ""
    return f"{sign} <b>{money(t['amount'])}</b> {t['emoji']} {t['category']}{where}{note}{day}{items}"


def render_saved(rows: list[dict], header: str = "") -> Reply:
    if not rows:
        return Reply(text="Не нашёл сумму. Напишите, например: <code>Пятёрочка 1450 продукты</code>")
    text = (header + "\n" if header else "") + "💾 Записал:\n" + "\n".join(_line(t) for t in rows)
    if len(rows) > 1:
        total = sum(t["amount"] for t in rows if t["kind"] == "expense")
        if total:
            text += f"\nИтого расходов: <b>{money(total)}</b>"
    if any(t.get("bank_dup") for t in rows):
        text += "\n🏦 Совпало с уведомлением банка — автоматическую запись убрал, чтобы не было дубля."
    buttons: list[list[Button]] = []
    if len(rows) == 1:
        tid = rows[0]["id"]
        buttons = [[Button(text="🏷 Категория", action=f"t:{tid}:cats"),
                    Button(text="❌ Отменить", action=f"t:{tid}:del")]]
        if rows[0].get("n_items"):
            buttons.insert(0, [Button(text="📋 Позиции чека", action=f"t:{tid}:items")])
    else:
        gid = rows[0].get("group_id")
        if gid:
            buttons = [[Button(text="📋 Позиции чека", action=f"g:{gid}:items")],
                       [Button(text="❌ Отменить весь чек", action=f"g:{gid}:del")]]
        else:
            buttons = [[Button(text=f"❌ {money(t['amount'])}", action=f"t:{t['id']}:del")] for t in rows]
    return Reply(text=text, buttons=buttons)


async def handle_action(user: User, parts: list[str]) -> Reply:
    """t:<id>:del | t:<id>:cats | t:<id>:c:<cat_id> | t:<id>:items | g:<uuid>:del | g:<uuid>:items"""
    pool = db.get_pool()
    if parts[2] == "items":
        return await _items_reply(user, parts[0], parts[1])
    if parts[0] == "g":
        async with pool.acquire() as conn, conn.transaction():
            n = await conn.fetchval(
                "WITH d AS (UPDATE transactions SET deleted_at=now() WHERE group_id=$1::uuid"
                " AND family_id=$2 AND deleted_at IS NULL RETURNING id) SELECT count(*) FROM d",
                parts[1], user.family_id)
            await db.emit(conn, "transaction.group_deleted", {"group_id": parts[1]})
        return Reply(text=f"🗑 Чек удалён ({plural(n, 'запись', 'записи', 'записей')}).")

    tx_id = int(parts[1])
    async with pool.acquire() as conn, conn.transaction():
        t = await _load(conn, tx_id)
        if not t or t["family_id"] != user.family_id or t["deleted_at"]:
            return Reply(text="Запись не найдена или уже удалена.")
        op = parts[2]
        if op == "del":
            await conn.execute("UPDATE transactions SET deleted_at=now() WHERE id=$1", tx_id)
            await db.emit(conn, "transaction.deleted", {"id": tx_id})
            from . import bills          # отмена оплаты регулярного платежа возвращает срок
            await bills.on_tx_cancelled(conn, [tx_id])
            return Reply(text=f"🗑 Отменено: {money(t['amount'])} {t['category']}")
        if op == "cats":
            cats = await categories(t["kind"])
            rows, row = [], []
            for c in cats:
                row.append(Button(text=f"{c['emoji']} {c['name']}", action=f"t:{tx_id}:c:{c['id']}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            return Reply(text=f"Выберите категорию для {money(t['amount'])}:", buttons=rows)
        if op == "c":
            await conn.execute("UPDATE transactions SET category_id=$2 WHERE id=$1", tx_id, int(parts[3]))
            t = await _load(conn, tx_id)
            await db.emit(conn, "transaction.updated", t)
            return render_saved([t])
    return Reply(text="Неизвестное действие.")


async def receipt_to_items(data: dict) -> list[dict]:
    """Чек → записи по категориям. Скидки распределяются пропорционально, чтобы сумма совпала с ИТОГ."""
    items = [i for i in (data.get("items") or []) if num(i.get("sum")) > 0]
    total = num(data.get("total")) or sum(num(i["sum"]) for i in items)
    common = {"kind": "expense", "merchant": data.get("merchant"), "date": data.get("date")}
    if not items:
        return [{**common, "amount": total, "category": "Продукты"}] if total > 0 else []
    by_cat: dict[str, Decimal] = {}
    lines: dict[str, list[dict]] = {}
    for i in items:
        cat = i.get("category") or "Прочее"
        by_cat[cat] = by_cat.get(cat, Decimal(0)) + num(i["sum"])
        lines.setdefault(cat, []).append(i)
    s = sum(by_cat.values())
    out = []
    rest = total
    cats = sorted(by_cat.items(), key=lambda kv: -kv[1])
    for n, (cat, v) in enumerate(cats):
        amount = rest if n == len(cats) - 1 else (v * total / s).quantize(Decimal("0.01"))
        rest -= amount
        out.append({**common, "amount": amount, "category": cat, "_items": lines[cat]})
    return out


async def _items_reply(user: User, kind: str, key: str) -> Reply:
    pool = db.get_pool()
    where = "t.group_id=$1::uuid" if kind == "g" else "t.id=$1::bigint"
    rows = await pool.fetch(
        "SELECT i.name, i.qty, i.amount, i.subcategory, c.emoji, c.name AS category, t.merchant"
        " FROM transaction_items i JOIN transactions t ON t.id=i.transaction_id JOIN categories c ON c.id=t.category_id"
        f" WHERE {where} AND t.family_id=$2 AND t.deleted_at IS NULL ORDER BY c.sort, i.amount DESC",
        key if kind == "g" else int(key), user.family_id)
    if not rows:
        return Reply(text="Позиции не найдены (чек удалён или распознан без позиций).")
    text = f"📋 <b>Позиции чека{': ' + esc(rows[0]['merchant']) if rows[0]['merchant'] else ''}</b>\n"
    cat = None
    for r in rows:
        if r["category"] != cat:
            cat = r["category"]
            text += f"\n{r['emoji']} <b>{esc(cat)}</b>\n"
        qty = f" ×{r['qty'].normalize():f}" if r["qty"] and r["qty"] != 1 else ""
        grp = f" <i>({esc(r['subcategory'])})</i>" if r["subcategory"] else ""
        text += f"• {esc(r['name'])}{qty}: {money(r['amount'])}{grp}\n"
    return Reply(text=text[:4000])


async def item_groups(family_id: int, start: date, end: date, limit: int = 6) -> list[dict]:
    """Топ групп товаров по позициям чеков за период."""
    rows = await db.get_pool().fetch(
        "SELECT coalesce(i.subcategory, 'другое') AS grp, sum(i.amount) AS total FROM transaction_items i"
        " JOIN transactions t ON t.id=i.transaction_id WHERE t.family_id=$1 AND t.deleted_at IS NULL"
        f" AND {scope.cond('finance', 't.user_id')}"
        " AND t.occurred_on >= $2 AND t.occurred_on < $3 GROUP BY 1 ORDER BY 2 DESC LIMIT $4",
        family_id, start, end, limit)
    return [dict(r) for r in rows]


def num(v) -> Decimal:
    try:
        return Decimal(str(v).replace(" ", "").replace(",", "."))
    except Exception:
        return Decimal(0)


async def month_summary(user: User, year: int, month: int) -> str:
    start = date(year, month, 1)
    end = date(year + (month == 12), month % 12 + 1, 1)
    pool = db.get_pool()
    rows = await pool.fetch(
        "SELECT t.kind, c.name, c.emoji, sum(t.amount) AS total FROM transactions t"
        " JOIN categories c ON c.id=t.category_id"
        " WHERE t.family_id=$1 AND t.deleted_at IS NULL AND t.occurred_on >= $2 AND t.occurred_on < $3"
        f" AND {scope.cond('finance', 't.user_id')}"
        " GROUP BY t.kind, c.name, c.emoji ORDER BY t.kind, total DESC", user.family_id, start, end)
    by_user = await pool.fetch(
        "SELECT u.name, t.kind, sum(t.amount) AS total FROM transactions t JOIN users u ON u.id=t.user_id"
        " WHERE t.family_id=$1 AND t.deleted_at IS NULL AND t.occurred_on >= $2 AND t.occurred_on < $3"
        f" AND {scope.cond('finance', 't.user_id')}"
        " GROUP BY u.name, t.kind ORDER BY u.name", user.family_id, start, end)
    months = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
              "сентябрь", "октябрь", "ноябрь", "декабрь"]
    if not rows:
        return f"💰 За {months[month - 1]} {year} записей пока нет."
    inc = sum(r["total"] for r in rows if r["kind"] == "income")
    exp = sum(r["total"] for r in rows if r["kind"] == "expense")
    text = f"💰 <b>{'Мои финансы' if scope.is_personal('finance') else 'Финансы семьи'}: {months[month - 1]} {year}</b>\n\n"
    text += f"Доходы: <b>{money(inc)}</b>\nРасходы: <b>{money(exp)}</b>\nОстаток: <b>{money(inc - exp)}</b>\n"
    if exp:
        text += "\n<b>Расходы по категориям</b>\n"
        for r in rows:
            if r["kind"] == "expense":
                pct = int(round(r["total"] * 100 / exp))
                text += f"{r['emoji']} {r['name']}: {money(r['total'])} ({pct}%)\n"
    if inc:
        text += "\n<b>Доходы</b>\n"
        for r in rows:
            if r["kind"] == "income":
                text += f"{r['emoji']} {r['name']}: {money(r['total'])}\n"
    groups = await item_groups(user.family_id, start, end)
    if groups:
        text += "\n<b>По чекам: группы товаров</b>\n"
        for g in groups:
            text += f"• {esc(g['grp'])}: {money(g['total'])}\n"
    if by_user and not scope.is_personal("finance"):
        text += "\n<b>Кто вносил</b>\n"
        people: dict[str, dict] = {}
        for r in by_user:
            people.setdefault(r["name"], {})[r["kind"]] = r["total"]
        for name, v in people.items():
            text += f"👤 {name}: расходы {money(v.get('expense', 0))}, доходы {money(v.get('income', 0))}\n"
    return text


async def today_expenses(user: User, day: date) -> tuple[Decimal, int]:
    row = await db.get_pool().fetchrow(
        "SELECT coalesce(sum(amount),0) AS s, count(*) AS n FROM transactions"
        " WHERE family_id=$1 AND kind='expense' AND deleted_at IS NULL AND occurred_on=$2"
        f" AND {scope.cond('finance', 'user_id')}",
        user.family_id, day)
    return row["s"], row["n"]
