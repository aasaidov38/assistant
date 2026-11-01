"""Учёт долгов (версия 1.7): кто нам должен и кому должны мы.

Долг — не трата и не доход: он хранится отдельно и не влияет на расходы и лимиты.
По умолчанию раздел личный: каждый видит свои долги (режим меняется в настройках).

Фразы: «дал Пете в долг 5000 до 15 ноября», «занял у Ивана 3000», «Петя вернул 2000», «отдал Ивану всё».
Кнопки: e:<id>:all (вернули всё) | e:<id>:later (напомнить через неделю) | e:<id>:forgive | e:0:list
"""
from datetime import date, timedelta
from decimal import Decimal

from . import db, notify, scope
from .config import get_settings
from .finance import money
from .schemas import Button, Reply
from .users import User
from .util import esc, now, num, parse_date, today

DIR = {"lent": ("💚", "мне должны", "должен(на) вам"), "borrowed": ("🟠", "я должен", "вы должны")}


async def debts(family_id: int, include_closed: bool = False, debt_id: int | None = None) -> list[dict]:
    rows = await db.get_pool().fetch(
        "SELECT d.*, u.name AS user_name,"
        " coalesce(sum(m.amount) FILTER (WHERE m.kind='add'), 0) AS added,"
        " coalesce(sum(m.amount) FILTER (WHERE m.kind IN ('repay','forgive')), 0) AS returned,"
        " max(m.occurred_on) AS last_move"
        " FROM debts d JOIN users u ON u.id=d.user_id"
        " LEFT JOIN debt_moves m ON m.debt_id=d.id AND m.deleted_at IS NULL"
        f" WHERE d.family_id=$1 AND d.deleted_at IS NULL AND {scope.cond('debts', 'd.user_id')}"
        " AND ($2::bigint IS NULL OR d.id=$2) GROUP BY d.id, u.name ORDER BY d.due_on NULLS LAST, d.created_at",
        family_id, debt_id)
    out = []
    for r in rows:
        d = dict(r)
        d["total"] = Decimal(str(d["amount"])) + Decimal(str(d["added"]))
        d["balance"] = d["total"] - Decimal(str(d["returned"]))
        d["closed"] = d["balance"] <= 0
        if include_closed or not d["closed"]:
            out.append(d)
    return out


async def get(user: User, debt_id: int) -> dict | None:
    rows = await debts(user.family_id, include_closed=True, debt_id=debt_id)
    return rows[0] if rows else None


def _norm(s: str | None) -> str:
    return (s or "").lower().replace("ё", "е").strip()


def _stem(w: str) -> str:
    return w[:max(3, len(w) - 2)]


async def find(user: User, person: str | None, direction: str | None = None) -> list[dict]:
    """Открытые долги по имени человека (Петя ≈ Пети ≈ Петей)."""
    ds = [d for d in await debts(user.family_id) if not direction or d["direction"] == direction]
    n = _norm(person)
    if not n:
        return ds if len(ds) == 1 else []
    exact = [d for d in ds if _norm(d["person"]) == n]
    if exact:
        return exact
    stems = [_stem(w) for w in n.split() if len(w) >= 2]
    return [d for d in ds if any(_stem(w).startswith(s) or s.startswith(_stem(w))
                                 for s in stems for w in _norm(d["person"]).split())]


def _due_text(d: dict) -> str:
    if not d["due_on"]:
        return ""
    left = (d["due_on"] - today()).days
    if left < 0:
        return f" · ⚠️ срок был {d['due_on']:%d.%m}"
    if left == 0:
        return " · срок сегодня"
    return f" · до {d['due_on']:%d.%m.%Y}" + (f" ({left} дн.)" if left <= 14 else "")


def line(d: dict, show_owner: bool = False) -> str:
    e, word, _ = DIR[d["direction"]]
    who = f" <i>({esc(d['user_name'])})</i>" if show_owner else ""
    if d["closed"]:
        return f"✅ {esc(d['person'])}: {money(d['total'])} — закрыт{who}"
    part = f" (вернули {money(d['returned'])} из {money(d['total'])})" if d["returned"] else ""
    return f"{e} <b>{esc(d['person'])}</b>: {money(d['balance'])}{part}{_due_text(d)}{who}"


def _buttons(d: dict) -> list[list[Button]]:
    if d["closed"]:
        return [[Button(text="🤝 Все долги", action="e:0:list")]]
    return [[Button(text="✅ Вернули всё" if d["direction"] == "lent" else "✅ Я вернул всё", action=f"e:{d['id']}:all"),
             Button(text="⏰ Через неделю", action=f"e:{d['id']}:later")],
            [Button(text="🤝 Все долги", action="e:0:list")]]


async def create(user: User, data: dict) -> Reply:
    direction = data.get("direction") if data.get("direction") in DIR else None
    person = str(data.get("person") or "").strip()[:80]
    amount = num(data.get("amount"))
    if not direction or not person or not amount or amount <= 0:
        return Reply(text="Напишите, кто, сколько и в какую сторону: <code>дал Пете в долг 5000 до 15 ноября</code> "
                          "или <code>занял у Ивана 3000</code>")
    due = parse_date(data.get("due"))
    same = [d for d in await find(user, person, direction) if d["user_id"] == user.id]
    pool = db.get_pool()
    if same:
        d = same[0]
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("INSERT INTO debt_moves (debt_id, user_id, kind, amount, note) VALUES ($1,$2,'add',$3,$4)",
                               d["id"], user.id, amount, data.get("note"))
            if due:
                await conn.execute("UPDATE debts SET due_on=$2, reminded='{}' WHERE id=$1", d["id"], due)
            await db.emit(conn, "debt.added", {"id": d["id"], "amount": amount})
        d = await get(user, d["id"])
        return Reply(text=f"➕ Добавил к долгу {money(amount)}\n" + line(d), buttons=_buttons(d))
    async with pool.acquire() as conn, conn.transaction():
        did = await conn.fetchval(
            "INSERT INTO debts (family_id, user_id, direction, person, amount, note, due_on) VALUES ($1,$2,$3,$4,$5,$6,$7)"
            " RETURNING id", user.family_id, user.id, direction, person, amount, data.get("note"), due)
        await db.emit(conn, "debt.created", {"id": did, "direction": direction, "amount": amount})
    d = await get(user, did)
    head = "🤝 Записал: " + ("вам должны" if direction == "lent" else "вы должны")
    tip = ("\nКогда вернут, напишите: <code>" + esc(person) + " вернул 1000</code>" if direction == "lent"
           else "\nКогда вернёте, напишите: <code>вернул " + esc(person) + " 1000</code>")
    if due:
        tip += "\n⏰ Напомню " + ("в день срока" if direction == "lent" else "за 3 дня и в день срока") + "."
    return Reply(text=f"{head}\n{line(d)}{tip}", buttons=_buttons(d))


async def _move(user: User, d: dict, kind: str, amount: Decimal, note: str | None = None) -> int:
    async with db.get_pool().acquire() as conn, conn.transaction():
        mid = await conn.fetchval("INSERT INTO debt_moves (debt_id, user_id, kind, amount, note) VALUES ($1,$2,$3,$4,$5)"
                                  " RETURNING id", d["id"], user.id, kind, amount, note)
        await db.emit(conn, f"debt.{kind}", {"id": d["id"], "amount": float(amount)})
    return mid


async def repay(user: User, data: dict) -> Reply:
    direction = data.get("direction") if data.get("direction") in DIR else None
    found = await find(user, data.get("person"), direction)
    if not found:
        ds = await debts(user.family_id)
        if not ds:
            return Reply(text="Открытых долгов нет. Записать: <code>дал Пете в долг 5000</code>")
        return Reply(text="Не нашёл такой долг. Сейчас открыты:\n" + "\n".join(line(d) for d in ds[:10]),
                     buttons=[[Button(text="🤝 Все долги", action="e:0:list")]])
    if len(found) > 1:
        return Reply(text="Уточните, какой долг:\n" + "\n".join(line(d) for d in found),
                     buttons=[[Button(text=f"✅ {d['person']}: всё", action=f"e:{d['id']}:all")] for d in found[:5]])
    d = found[0]
    amount = num(data.get("amount"))
    a = Decimal(str(amount)) if amount and amount > 0 else d["balance"]
    extra = ""
    if a > d["balance"]:
        extra = f"\nℹ️ Вернули больше долга на {money(a - d['balance'])} — записал только остаток."
        a = d["balance"]
    await _move(user, d, "repay", a)
    d = await get(user, d["id"])
    head = "✅ Долг закрыт!" if d["closed"] else f"💸 Вернули {money(a)}"
    return Reply(text=f"{head}\n{line(d)}{extra}", buttons=_buttons(d))


async def forgive(user: User, debt_id: int) -> Reply:
    d = await get(user, debt_id)
    if not d or d["closed"]:
        return Reply(text="Долг уже закрыт.")
    await _move(user, d, "forgive", d["balance"], "простили остаток")
    d = await get(user, debt_id)
    return Reply(text="🕊 Остаток долга списан.\n" + line(d), buttons=_buttons(d))


def totals(ds: list[dict]) -> dict:
    return {"lent": sum((d["balance"] for d in ds if d["direction"] == "lent" and not d["closed"]), Decimal(0)),
            "borrowed": sum((d["balance"] for d in ds if d["direction"] == "borrowed" and not d["closed"]), Decimal(0))}


async def list_reply(user: User, head: str = "") -> Reply:
    ds = await debts(user.family_id)
    top = (head + "\n\n" if head else "")
    if not ds:
        return Reply(text=top + "🤝 Открытых долгов нет.\nЗаписать: <code>дал Пете в долг 5000 до 15 ноября</code> "
                                "или <code>занял у Ивана 3000</code>")
    shared = not scope.is_personal("debts")
    t = totals(ds)
    text = top + "🤝 <b>Долги</b>\n"
    for key, title in (("lent", "💚 Мне должны"), ("borrowed", "🟠 Я должен")):
        part = [d for d in ds if d["direction"] == key]
        if part:
            text += f"\n<b>{title}: {money(t[key])}</b>\n" + "\n".join(line(d, shared) for d in part) + "\n"
    buttons = [[Button(text=f"✅ {d['person'][:18]}: {'вернули' if d['direction'] == 'lent' else 'вернул'} всё",
                       action=f"e:{d['id']}:all")] for d in ds[:6]]
    return Reply(text=text.strip(), buttons=buttons)


async def handle_action(user: User, parts: list[str]) -> Reply:
    if parts[2] == "list":
        return await list_reply(user)
    did = int(parts[1])
    d = await get(user, did)
    if not d:
        return Reply(text="Долг не найден.")
    if parts[2] == "all":
        if d["closed"]:
            return Reply(text="Долг уже закрыт.\n" + line(d))
        return await _repay_one(user, d)
    if parts[2] == "later":
        until = today() + timedelta(days=7)
        await db.get_pool().execute("UPDATE debts SET snooze_until=$2 WHERE id=$1", did, until)
        return Reply(text=f"⏰ Напомню {until:%d.%m}.\n" + line(d))
    if parts[2] == "forgive":
        return await forgive(user, did)
    return Reply(text="Неизвестное действие.")


async def _repay_one(user: User, d: dict) -> Reply:
    await _move(user, d, "repay", d["balance"])
    d = await get(user, d["id"])
    return Reply(text="✅ Долг закрыт!\n" + line(d), buttons=_buttons(d))


def _need(d: dict, day: date) -> str | None:
    """Ключ напоминания на сегодня или None."""
    if d["closed"] or not d["due_on"]:
        return None
    if d["snooze_until"] and day < d["snooze_until"]:
        return None
    left = (d["due_on"] - day).days
    if d["direction"] == "borrowed" and 1 <= left <= 3:
        key = "pre3"
    elif left == 0:
        key = "due"
    elif left < 0:
        key = f"over:{(-left) // 7}"      # просрочка: раз в неделю
    else:
        return None
    if d["snooze_until"] and day >= d["snooze_until"]:
        key += f":s{d['snooze_until']}"
    return None if key in (d["reminded"] or []) else key


async def send_due() -> None:
    """Раз в день после DAILY_REMINDER_HOUR: сроки возврата долгов. Напоминание получает тот, кто записал долг."""
    n = now()
    if n.hour < get_settings().daily_reminder_hour:
        return
    pool = db.get_pool()
    fams = await pool.fetch("SELECT DISTINCT family_id FROM debts WHERE deleted_at IS NULL AND due_on IS NOT NULL")
    for f in fams:
        for d in await debts(f["family_id"]):
            key = _need(d, n.date())
            if not key:
                continue
            ok = await pool.fetchval("UPDATE debts SET reminded = array_append(reminded, $2) WHERE id=$1"
                                     " AND NOT ($2 = ANY(reminded)) RETURNING id", d["id"], key)
            if not ok:
                continue
            if d["direction"] == "lent":
                text = (f"🤝 Сегодня {esc(d['person'])} обещал(а) вернуть {money(d['balance'])}." if key.startswith("due")
                        else f"🤝 {esc(d['person'])} не вернул(а) {money(d['balance'])}, срок был {d['due_on']:%d.%m}.")
            else:
                text = (f"🤝 Через {(d['due_on'] - n.date()).days} дн. вернуть {esc(d['person'])} {money(d['balance'])}."
                        if key.startswith("pre3") else
                        f"🤝 Сегодня срок: вернуть {esc(d['person'])} {money(d['balance'])}." if key.startswith("due") else
                        f"🤝 Долг {esc(d['person'])} {money(d['balance'])} просрочен (срок был {d['due_on']:%d.%m}).")
            await notify.send(d["user_id"], Reply(text=text, buttons=_buttons(d)), topic="debts")


async def morning_lines(user: User) -> list[str]:
    d0 = today()
    out = []
    for d in await debts(user.family_id):
        if d["due_on"] and d["user_id"] == user.id and (d["due_on"] - d0).days <= (3 if d["direction"] == "borrowed" else 0):
            out.append("🤝 " + esc(d["person"]) + (": вернуть " if d["direction"] == "borrowed" else ": ждём возврат ")
                       + money(d["balance"]) + _due_text(d))
    return out[:4]
