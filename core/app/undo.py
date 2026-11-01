"""Отмена и возврат записей: деньги, еда, привычки, дела, покупки.

Записи не стираются: при отмене у них ставится deleted_at, поэтому отменённое можно вернуть.
Исключение — дела: при отмене дело удаляется и из TickTick, вернуть его нельзя.

Ссылка на операцию («ref»): <тип>.<id>
  t.123   трата или доход        g.<uuid> чек целиком (все позиции)
  m.45    приём пищи             h.7      показатель (вода, вес, шаги, сон)
  k.9     дело                   s.12     покупка в списке
Кнопки в Telegram: u:<ref>:del | u:<ref>:back | u:0:list  (в конце :l — ответ списком)
"""
import re
from datetime import datetime, timedelta
from decimal import Decimal

from . import bills, db, garage, habits, scope, tasks, users
from .finance import money
from .schemas import Button, Reply
from .util import esc, now, today

TYPES = {"finance": "💰 Деньги", "meal": "🍽 Питание", "metric": "📈 Привычки",
         "task": "📝 Дела", "shopping": "🛒 Покупки", "savings": "🎯 Накопления", "garage": "🏍 Гараж",
         "paper": "📄 Документы", "meter": "🔢 Счётчики", "note": "🗒 Заметки", "debt": "🤝 Долги"}
_PREFIX_TYPE = {"t": "finance", "g": "finance", "m": "meal", "h": "metric", "k": "task", "s": "shopping",
                "v": "savings", "o": "garage", "l": "garage", "d": "paper", "r": "meter", "n": "note",
                "e": "debt", "p": "debt"}
# К какому разделу (scope.SECTIONS) относится тип записи: в личном разделе чужие записи не видны
TYPE_SECTION = {"finance": "finance", "meal": "nutrition", "metric": "habits", "shopping": "shopping", "savings": "savings",
                "garage": "garage", "paper": "papers", "meter": "meters", "note": "notes", "debt": "debts"}
REF_RE = re.compile(r"^(?:[tmhksvoldrnep]\.\d{1,18}|g\.[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")

# Простые записи версии 1.5: (таблица с алиасом x, join, поле семьи, поле автора, заголовок SQL, значение SQL)
SIMPLE = {
    "v": ("savings_moves x JOIN savings_goals g ON g.id=x.goal_id", "g.family_id", "x.user_id",
          "'🎯 ' || g.title", "x.amount", "money_signed"),
    "o": ("odometer x JOIN vehicles g ON g.id=x.vehicle_id", "g.family_id", "x.user_id",
          "'🏍 ' || g.name || ': пробег'", "x.km", "km"),
    "l": ("service_log x JOIN vehicles g ON g.id=x.vehicle_id", "g.family_id", "x.user_id",
          "'🔧 ' || g.name || ': ' || x.title", "x.cost", "money"),
    "d": ("papers x", "x.family_id", "x.user_id", "'📄 ' || x.title || coalesce(' (' || x.owner || ')', '')",
          "x.expires_on", "date"),
    "r": ("meter_readings x LEFT JOIN meters g ON g.id=x.meter_id", "x.family_id", "x.user_id",
          "'🔢 ' || coalesce(g.title, 'Счётчик не выбран')", "x.value", "num"),
    "n": ("family_notes x", "x.family_id", "x.user_id", "'🗒 ' || coalesce(x.subject || ': ', '') || left(x.text, 60)",
          "NULL::text", "none"),
    "e": ("debts x", "x.family_id", "x.user_id",
          "'🤝 ' || CASE x.direction WHEN 'lent' THEN 'Дал в долг: ' ELSE 'Взял в долг: ' END || x.person", "x.amount", "money"),
    "p": ("debt_moves x JOIN debts g ON g.id=x.debt_id", "g.family_id", "x.user_id",
          "'🤝 ' || g.person || CASE x.kind WHEN 'repay' THEN ': возврат' WHEN 'add' THEN ': добавили к долгу'"
          " ELSE ': остаток списан' END", "x.amount", "money"),
}
_SIMPLE_TABLE = {"v": "savings_moves", "o": "odometer", "l": "service_log", "d": "papers", "r": "meter_readings",
                 "n": "family_notes",
                 "e": "debts", "p": "debt_moves"}
LOOKBACK_DAYS = 60          # «отмени последнюю…» ищет среди записей за этот срок


def valid_ref(ref: str) -> bool:
    return bool(REF_RE.match(ref or ""))


# ---------- Чтение операций ----------

def _num(v) -> str:
    v = Decimal(str(v)).normalize()
    return f"{v:f}".replace(".", ",")


async def _finance(family_id, days, user_id, limit, ref=None):
    where, args = "", [family_id, days, user_id, limit]
    # Обычно позиции одного чека показываются одной операцией (g.<uuid>).
    # Но одну строку чека тоже можно отменить отдельно (t.<id>, например из таблицы операций в панели).
    single = bool(ref and ref[0] == "t")
    grp_key = "t.id" if single else "t.group_id, CASE WHEN t.group_id IS NULL THEN t.id END"
    ref_expr = ("'t.' || t.id" if single else
                "CASE WHEN t.group_id IS NULL THEN 't.' || min(t.id) ELSE 'g.' || t.group_id::text END")
    is_group = "false" if single else "t.group_id IS NOT NULL"
    if single:
        where, args = " AND t.id=$5", args + [int(ref[2:])]
    elif ref and ref[0] == "g":
        where, args = " AND t.group_id=$5::uuid", args + [ref[2:]]
    rows = await db.get_pool().fetch(
        f"SELECT {ref_expr} AS ref,"
        " min(t.user_id) AS user_id, min(t.created_at) AS at, bool_and(t.deleted_at IS NOT NULL) AS cancelled,"
        " max(t.deleted_at) AS cancelled_at, count(*) FILTER (WHERE t.deleted_at IS NULL) AS n_live, count(*) AS n,"
        " coalesce(sum(t.amount) FILTER (WHERE t.deleted_at IS NULL), sum(t.amount)) AS amount, min(t.kind) AS kind,"
        " min(t.merchant) AS merchant, min(c.emoji) AS emoji, min(c.name) AS category, min(t.note) AS note,"
        f" {is_group} AS is_group, min(t.occurred_on) AS occurred_on"
        " FROM transactions t JOIN categories c ON c.id=t.category_id"
        " WHERE t.family_id=$1 AND t.created_at > now() - make_interval(days => $2)"
        " AND ($3::int IS NULL OR t.user_id=$3)" + where +
        f" GROUP BY {grp_key} ORDER BY at DESC LIMIT $4", *args)
    out = []
    for r in rows:
        where_ = f" · {r['merchant']}" if r["merchant"] else ""
        if r["is_group"]:
            n = r["n_live"] if not r["cancelled"] else r["n"]
            title = f"🧾 Чек{where_} ({n} поз.)"
        else:
            title = f"{r['emoji']} {r['category']}{where_}" + (f" · {r['note']}" if r["note"] and not r["merchant"] else "")
        sign = "+" if r["kind"] == "income" else "−"
        out.append({"ref": r["ref"], "type": "finance", "user_id": r["user_id"], "at": r["at"],
                    "cancelled": r["cancelled"], "cancelled_at": r["cancelled_at"], "title": title,
                    "amount": float(r["amount"]), "kind": r["kind"], "value": f"{sign}{money(r['amount'])}",
                    "search": " ".join(filter(None, [r["category"], r["merchant"], r["note"], "чек" if r["is_group"] else ""])),
                    "can_cancel": not r["cancelled"], "can_restore": r["cancelled"]})
    return out


async def _meals(family_id, days, user_id, limit, ref=None):
    where, args = "", [family_id, days, user_id, limit]
    if ref:
        where, args = " AND m.id=$5", args + [int(ref[2:])]
    rows = await db.get_pool().fetch(
        "SELECT 'm.' || m.id AS ref, m.user_id, m.created_at AS at, m.deleted_at IS NOT NULL AS cancelled,"
        " m.deleted_at AS cancelled_at, m.title, m.kcal FROM meals m JOIN users u ON u.id=m.user_id"
        " WHERE u.family_id=$1 AND m.created_at > now() - make_interval(days => $2)"
        " AND ($3::int IS NULL OR m.user_id=$3)" + where + " ORDER BY m.created_at DESC LIMIT $4", *args)
    return [{"ref": r["ref"], "type": "meal", "user_id": r["user_id"], "at": r["at"], "cancelled": r["cancelled"],
             "cancelled_at": r["cancelled_at"], "title": f"🍽 {r['title']}", "value": f"{round(r['kcal'])} ккал",
             "search": r["title"], "can_cancel": not r["cancelled"], "can_restore": r["cancelled"]} for r in rows]


async def _metrics(family_id, days, user_id, limit, ref=None):
    where, args = "", [family_id, days, user_id, limit]
    if ref:
        where, args = " AND x.id=$5", args + [int(ref[2:])]
    rows = await db.get_pool().fetch(
        "SELECT 'h.' || x.id AS ref, x.user_id, x.created_at AS at, x.deleted_at IS NOT NULL AS cancelled,"
        " x.deleted_at AS cancelled_at, x.kind, x.value FROM metrics x JOIN users u ON u.id=x.user_id"
        " WHERE u.family_id=$1 AND x.created_at > now() - make_interval(days => $2)"
        " AND ($3::int IS NULL OR x.user_id=$3)" + where + " ORDER BY x.created_at DESC LIMIT $4", *args)
    out = []
    for r in rows:
        emoji, name, unit, *_ = habits.KINDS.get(r["kind"], ("📈", r["kind"], "", None, "sum"))
        out.append({"ref": r["ref"], "type": "metric", "metric": r["kind"], "user_id": r["user_id"], "at": r["at"],
                    "cancelled": r["cancelled"], "cancelled_at": r["cancelled_at"], "title": f"{emoji} {name}",
                    "value": f"{_num(r['value'])} {unit}".strip(), "search": name,
                    "can_cancel": not r["cancelled"], "can_restore": r["cancelled"]})
    return out


async def _tasks(family_id, days, user_id, limit, ref=None):
    where, args = "", [family_id, days, user_id, limit]
    if ref:
        where, args = " AND t.id=$5", args + [int(ref[2:])]
    rows = await db.get_pool().fetch(
        "SELECT 'k.' || t.id AS ref, t.user_id, t.created_at AS at, t.status, t.updated_at, t.title, t.due_at"
        " FROM tasks t WHERE t.family_id=$1 AND coalesce(t.source, '') <> 'ticktick'"
        " AND t.created_at > now() - make_interval(days => $2)"
        " AND ($3::int IS NULL OR t.user_id=$3)" + where + " ORDER BY t.created_at DESC LIMIT $4", *args)
    out = []
    for r in rows:
        st = r["status"]
        out.append({"ref": r["ref"], "type": "task", "user_id": r["user_id"], "at": r["at"],
                    "cancelled": st == "deleted", "cancelled_at": r["updated_at"] if st == "deleted" else None,
                    "done": st == "done", "title": f"📝 {r['title']}",
                    "value": _when(r["due_at"]) if r["due_at"] else "", "search": r["title"],
                    "can_cancel": st == "open", "can_restore": False})
    return out


async def _shopping(family_id, days, user_id, limit, ref=None):
    where, args = "", [family_id, days, user_id, limit]
    if ref:
        where, args = " AND s.id=$5", args + [int(ref[2:])]
    rows = await db.get_pool().fetch(
        "SELECT 's.' || s.id AS ref, s.added_by AS user_id, s.created_at AS at, s.deleted_at IS NOT NULL AS cancelled,"
        " s.deleted_at AS cancelled_at, s.bought_at, s.title, s.qty FROM shopping_items s"
        " WHERE s.family_id=$1 AND s.created_at > now() - make_interval(days => $2)"
        " AND ($3::int IS NULL OR s.added_by=$3)" + where + " ORDER BY s.created_at DESC LIMIT $4", *args)
    return [{"ref": r["ref"], "type": "shopping", "user_id": r["user_id"], "at": r["at"], "cancelled": r["cancelled"],
             "cancelled_at": r["cancelled_at"], "done": r["bought_at"] is not None,
             "title": f"🛒 {r['title']}", "value": r["qty"] or "", "search": r["title"],
             "can_cancel": not r["cancelled"] and r["bought_at"] is None, "can_restore": r["cancelled"]} for r in rows]


def _fmt(v, how: str) -> str:
    if v is None or how == "none":
        return ""
    if how == "money_signed":
        return ("+" if v > 0 else "−") + money(abs(v))
    if how == "money":
        return money(v)
    if how == "km":
        return f"{int(v):,} км".replace(",", " ")
    if how == "date":
        return f"до {v:%d.%m.%Y}" if hasattr(v, "strftime") else str(v)
    return _num(v)


async def _simple(prefix: str, family_id, days, user_id, limit, ref=None):
    table, fam, author, title, value, how = SIMPLE[prefix]
    where, args = "", [family_id, days, user_id, limit]
    if ref:
        where, args = " AND x.id=$5", args + [int(ref[2:])]
    rows = await db.get_pool().fetch(
        f"SELECT x.id, {author} AS user_id, x.created_at AS at, x.deleted_at IS NOT NULL AS cancelled,"
        f" x.deleted_at AS cancelled_at, {title} AS title, {value} AS value FROM {table}"
        f" WHERE {fam}=$1 AND x.created_at > now() - make_interval(days => $2) AND ($3::int IS NULL OR {author}=$3)"
        + where + " ORDER BY x.created_at DESC LIMIT $4", *args)
    return [{"ref": f"{prefix}.{r['id']}", "type": _PREFIX_TYPE[prefix], "user_id": r["user_id"], "at": r["at"],
             "cancelled": r["cancelled"], "cancelled_at": r["cancelled_at"], "title": r["title"],
             "value": _fmt(r["value"], how), "search": r["title"], "can_cancel": not r["cancelled"],
             "can_restore": r["cancelled"]} for r in rows]


def _simple_fetch(*prefixes):
    async def f(family_id, days, user_id, limit, ref=None):
        out = []
        for p in prefixes:
            if ref is None or ref[0] == p:
                out += await _simple(p, family_id, days, user_id, limit, ref)
        return out
    return f


_FETCH = {"finance": _finance, "meal": _meals, "metric": _metrics, "task": _tasks, "shopping": _shopping,
          "savings": _simple_fetch("v"), "garage": _simple_fetch("o", "l"), "paper": _simple_fetch("d"),
          "meter": _simple_fetch("r"), "note": _simple_fetch("n"), "debt": _simple_fetch("e", "p")}


async def history(family_id: int, *, user_id: int | None = None, types: list[str] | None = None,
                  days: int = 30, limit: int = 100) -> list[dict]:
    """Операции семьи (или одного человека) за days дней, новые сверху. Отменённые тоже, с пометкой."""
    days, limit = max(1, min(days, 365)), max(1, min(limit, 500))
    out: list[dict] = []
    for t in (types or list(_FETCH)):
        if t in _FETCH:
            out += await _FETCH[t](family_id, days, user_id, limit)
    out = [o for o in out if scope.can_see(TYPE_SECTION.get(o["type"]), o["user_id"])]
    out.sort(key=lambda o: o["at"], reverse=True)
    out = out[:limit]
    names = {m.id: m.name for m in await users.family_members(family_id)}
    for o in out:
        o["user_name"] = names.get(o["user_id"], "")
    return out


async def get(family_id: int, ref: str) -> dict | None:
    if not valid_ref(ref):
        return None
    rows = await _FETCH[_PREFIX_TYPE[ref[0]]](family_id, 3650, None, 1, ref)
    if not rows:
        return None
    o = rows[0]
    if not scope.can_see(TYPE_SECTION.get(o["type"]), o["user_id"]):
        return None
    o["user_name"] = next((m.name for m in await users.family_members(family_id) if m.id == o["user_id"]), "")
    return o


# ---------- Отмена и возврат ----------

_TABLE = {"m": ("meals", "meal"), "h": ("metrics", "metric"), "s": ("shopping_items", "shopping")}


async def cancel(user: users.User, ref: str, via: str = "bot") -> tuple[dict | None, str]:
    """Отменяет операцию. Возвращает (операция после отмены, дополнительная пометка) или (None, причина)."""
    op = await get(user.family_id, ref)
    if not op:
        return None, "Запись не найдена."
    if not op["can_cancel"]:
        return None, "Эту запись уже отменили." if op["cancelled"] else "Эту запись отменить нельзя."
    p, key = ref[0], ref[2:]
    note = ""
    if p == "k":
        r = await tasks.handle_action(user, ["k", key, "del"])
        if "📲" in r.text or "⚠️" in r.text:
            note = r.text[r.text.find("\n") + 1:] if "\n" in r.text else ""
    elif p in SIMPLE:
        async with db.get_pool().acquire() as conn, conn.transaction():
            n = await conn.execute(f"UPDATE {_SIMPLE_TABLE[p]} SET deleted_at=now() WHERE id=$1 AND deleted_at IS NULL",
                                   int(key))
            await _after_simple(conn, p, int(key))
            await db.emit(conn, f"{op['type']}.deleted", {"ref": ref, "via": via})
        if n.endswith(" 0"):
            return None, "Эту запись уже отменили."
    else:
        async with db.get_pool().acquire() as conn, conn.transaction():
            if p == "t":
                n = await conn.execute("UPDATE transactions SET deleted_at=now() WHERE id=$1 AND family_id=$2"
                                       " AND deleted_at IS NULL", int(key), user.family_id)
                await db.emit(conn, "transaction.deleted", {"id": int(key), "via": via})
                await bills.on_tx_cancelled(conn, [int(key)])
            elif p == "g":
                n = await conn.execute("UPDATE transactions SET deleted_at=now() WHERE group_id=$1::uuid AND family_id=$2"
                                       " AND deleted_at IS NULL", key, user.family_id)
                await db.emit(conn, "transaction.group_deleted", {"group_id": key, "via": via})
            elif p == "s":
                n = await conn.execute("UPDATE shopping_items SET deleted_at=now() WHERE id=$1 AND family_id=$2"
                                       " AND deleted_at IS NULL", int(key), user.family_id)
                await db.emit(conn, "shopping.deleted", {"id": int(key), "via": via})
            else:
                table, ev = _TABLE[p]
                n = await conn.execute(f"UPDATE {table} x SET deleted_at=now() FROM users u WHERE x.id=$1"
                                       " AND u.id=x.user_id AND u.family_id=$2 AND x.deleted_at IS NULL",
                                       int(key), user.family_id)
                await db.emit(conn, f"{ev}.deleted", {"id": int(key), "via": via})
        if n.endswith(" 0"):
            return None, "Эту запись уже отменили."
    return await get(user.family_id, ref), note


async def restore(user: users.User, ref: str, via: str = "bot") -> tuple[dict | None, str]:
    """Возвращает отменённую запись."""
    op = await get(user.family_id, ref)
    if not op:
        return None, "Запись не найдена."
    if not op["can_restore"]:
        if ref[0] == "k":
            return None, "Удалённое дело вернуть нельзя: оно удалено и из TickTick. Создайте его заново."
        return None, "Запись и так действует."
    p, key = ref[0], ref[2:]
    async with db.get_pool().acquire() as conn, conn.transaction():
        if p in SIMPLE:
            await conn.execute(f"UPDATE {_SIMPLE_TABLE[p]} SET deleted_at=NULL WHERE id=$1", int(key))
            await _after_simple(conn, p, int(key))
            await db.emit(conn, f"{op['type']}.restored", {"ref": ref, "via": via})
        elif p == "t":
            await conn.execute("UPDATE transactions SET deleted_at=NULL WHERE id=$1 AND family_id=$2", int(key), user.family_id)
            await db.emit(conn, "transaction.restored", {"id": int(key), "via": via})
            await bills.on_tx_restored(conn, [int(key)])
        elif p == "g":
            # возвращаем только то, что отменили вместе (позиции, удалённые раньше по одной, не трогаем)
            await conn.execute(
                "UPDATE transactions SET deleted_at=NULL WHERE group_id=$1::uuid AND family_id=$2 AND deleted_at ="
                " (SELECT max(deleted_at) FROM transactions WHERE group_id=$1::uuid AND family_id=$2)", key, user.family_id)
            await db.emit(conn, "transaction.group_restored", {"group_id": key, "via": via})
        elif p == "s":
            await conn.execute("UPDATE shopping_items SET deleted_at=NULL WHERE id=$1 AND family_id=$2", int(key), user.family_id)
            await db.emit(conn, "shopping.restored", {"id": int(key), "via": via})
        else:
            table, ev = _TABLE[p]
            await conn.execute(f"UPDATE {table} x SET deleted_at=NULL FROM users u WHERE x.id=$1"
                               " AND u.id=x.user_id AND u.family_id=$2", int(key), user.family_id)
            await db.emit(conn, f"{ev}.restored", {"id": int(key), "via": via})
    return await get(user.family_id, ref), ""


async def _after_simple(conn, p: str, rid: int) -> None:
    """Пересчёт связанных данных после отмены или возврата."""
    if p == "o":
        vid = await conn.fetchval("SELECT vehicle_id FROM odometer WHERE id=$1", rid)
        await garage.recalc_mileage(conn, vid)
    elif p == "l":
        r = await conn.fetchrow("SELECT rule_id, transaction_id, prev_km, prev_date FROM service_log WHERE id=$1", rid)
        if r["rule_id"]:
            last = await conn.fetchrow("SELECT km, done_on FROM service_log WHERE rule_id=$1 AND deleted_at IS NULL"
                                       " ORDER BY done_on DESC, id DESC LIMIT 1", r["rule_id"])
            km, d = (last["km"], last["done_on"]) if last else (r["prev_km"], r["prev_date"])
            await conn.execute("UPDATE service_rules SET last_km=$2, last_date=$3 WHERE id=$1", r["rule_id"], km, d)
        if r["transaction_id"]:      # трата за ТО отменяется и возвращается вместе с записью
            await conn.execute("UPDATE transactions SET deleted_at = CASE WHEN (SELECT deleted_at FROM service_log WHERE id=$2)"
                               " IS NULL THEN NULL ELSE now() END WHERE id=$1", r["transaction_id"], rid)


# ---------- Текст для бота ----------

def _when(dt: datetime | None) -> str:
    if not dt:
        return ""
    d = dt.astimezone(now().tzinfo)
    day = d.date()
    t = today()
    prefix = "сегодня" if day == t else "вчера" if day == t - timedelta(days=1) else f"{d:%d.%m}"
    return f"{prefix} {d:%H:%M}"


def describe(op: dict, me: users.User | None = None, html: bool = True) -> str:
    e = esc if html else (lambda s: s or "")
    s = e(op["title"])
    if op.get("value"):
        s += f" · {e(op['value'])}" if op["type"] != "finance" else f" {e(op['value'])}"
    who = f", {e(op['user_name'])}" if me and op.get("user_name") and op["user_id"] != me.id else ""
    return f"{s} <i>({_when(op['at'])}{who})</i>" if html else f"{s} ({_when(op['at'])}{who})"


def _short(op: dict) -> str:
    t = re.sub(r"^\W+\s*", "", op["title"])
    t = (t[:18] + "…") if len(t) > 19 else t
    return f"{t} {op.get('value', '')}".strip()[:32]


async def list_reply(user: users.User, notice: str = "") -> Reply:
    """Последние записи пользователя с кнопками «отменить» / «вернуть»."""
    ops = await history(user.family_id, user_id=user.id, days=LOOKBACK_DAYS, limit=8)
    head = (notice + "\n\n") if notice else ""
    if not ops:
        return Reply(text=head + "🕘 За последние два месяца ваших записей нет.")
    lines, rows, row = [], [], []
    for i, op in enumerate(ops, 1):
        d = describe(op, user)
        if op["cancelled"]:
            d = f"<s>{d}</s> — отменено"
        elif op.get("done"):
            d += " — " + ("выполнено" if op["type"] == "task" else "куплено")
        lines.append(f"{i}. {d}")
        btn = None
        if op["can_cancel"]:
            btn = Button(text=f"❌ {i}. {_short(op)}", action=f"u:{op['ref']}:del:l")
        elif op["can_restore"]:
            btn = Button(text=f"↩️ {i}. Вернуть", action=f"u:{op['ref']}:back:l")
        if btn:
            rows.append([btn])
    return Reply(text=head + "🕘 <b>Ваши последние записи</b>\n" + "\n".join(lines)
                 + "\n\n❌ — отменить запись, ↩️ — вернуть отменённую.", buttons=rows)


def _cancelled_reply(op: dict, note: str, user: users.User) -> Reply:
    text = f"🗑 Отменил: {describe(op, user)}" + (f"\n{note}" if note else "")
    buttons = []
    if op["can_restore"]:
        buttons.append([Button(text="↩️ Вернуть", action=f"u:{op['ref']}:back")])
    buttons.append([Button(text="🕘 Последние записи", action="u:0:list")])
    return Reply(text=text, buttons=buttons)


async def undo_last(user: users.User, what: str = "any", metric: str | None = None,
                    kind: str | None = None, group: bool = False, query: str | None = None) -> Reply:
    """«Отмени последнюю операцию / трату / еду / задачу…». Отменяет самую свежую запись пользователя."""
    types = [what] if what in _FETCH else None
    ops = await history(user.family_id, user_id=user.id, types=types, days=LOOKBACK_DAYS, limit=200)
    q = (query or "").lower().replace("ё", "е").strip()
    words = [w for w in re.findall(r"\w{3,}", q)]

    def fits(o):
        if not o["can_cancel"]:
            return False
        if metric and o.get("metric") != metric:
            return False
        if kind and o.get("kind") != kind:
            return False
        if group and not o["ref"].startswith("g."):
            return False
        if words:
            hay = (o["search"] + " " + o["title"]).lower().replace("ё", "е")
            return all(w[:max(4, len(w) - 2)] in hay for w in words)   # «кофе» найдёт «Кофе с молоком»
        return True

    op = next((o for o in ops if fits(o)), None)
    if not op:
        what_txt = {"finance": " с деньгами", "meal": " о еде", "metric": " о привычках", "task": " с делами",
                    "shopping": " в списке покупок"}.get(what, "")
        extra = f" по запросу «{esc(query)}»" if words else ""
        return Reply(text=f"🤷 Не нашёл ваших записей{what_txt}{extra} за последние {LOOKBACK_DAYS} дней, которые можно отменить.",
                     buttons=[[Button(text="🕘 Последние записи", action="u:0:list")]])
    done, note = await cancel(user, op["ref"])
    if not done:
        return Reply(text=f"⚠️ {esc(note)}")
    return _cancelled_reply(done, note, user)


async def handle_action(user: users.User, parts: list[str]) -> Reply:
    """u:<ref>:del | u:<ref>:back | u:0:list ; в конце :l — ответить обновлённым списком."""
    ref, op = parts[1], parts[2]
    as_list = len(parts) > 3 and parts[3] == "l"
    if op == "list":
        return await list_reply(user)
    if not valid_ref(ref):
        return Reply(text="Кнопка устарела.")
    if op == "del":
        done, note = await cancel(user, ref)
        if not done:
            return await list_reply(user, f"⚠️ {esc(note)}") if as_list else Reply(text=f"⚠️ {esc(note)}")
        if as_list:
            return await list_reply(user, f"🗑 Отменил: {describe(done, user)}" + (f"\n{note}" if note else ""))
        return _cancelled_reply(done, note, user)
    if op == "back":
        done, note = await restore(user, ref)
        if not done:
            return await list_reply(user, f"⚠️ {esc(note)}") if as_list else Reply(text=f"⚠️ {esc(note)}")
        text = f"↩️ Вернул: {describe(done, user)}"
        if as_list:
            return await list_reply(user, text)
        return Reply(text=text, buttons=[[Button(text="❌ Отменить", action=f"u:{ref}:del")],
                                         [Button(text="🕘 Последние записи", action="u:0:list")]])
    return Reply(text="Неизвестное действие.")


# ---------- Распознавание команды без ИИ ----------

_UNDO_VERB = re.compile(r"^(?:/undo|/cancel|отмен\w*|удали\w*|убери|убрать|сотри|стереть)(?:\s+|$)(.*)$")
_LIST_CMD = re.compile(r"^(?:/history|/log|история|журнал|последние (?:операции|записи)|мои (?:записи|операции))$")
_FILLER = ("последн", "мою", "мой", "мое", "мои", "мне", "запис", "операц", "действи", "это", "ее", "его",
           "пожалуйст", "плиз", "из", "в", "на", "то", "ту", "тот", "только", "что", "сейчас", "по", "ошибк",
           "ошибоч", "добавлен", "введен", "внесен", "сделан", "пожалуй", "мой", "крайн", "свою", "свой")
# (что ищем, уточнение) по началу слова; порядок важен: «покупок» раньше «покупк»
_KINDS = [("покупок", ("shopping", {})), ("списк", ("shopping", {})),
          ("доход", ("finance", {"kind": "income"})), ("зарплат", ("finance", {"kind": "income"})),
          ("трат", ("finance", {"kind": "expense"})), ("расход", ("finance", {"kind": "expense"})),
          ("покупк", ("finance", {"kind": "expense"})), ("чек", ("finance", {"group": True})),
          ("платеж", ("finance", {})), ("деньг", ("finance", {})), ("сумм", ("finance", {})),
          ("прием", ("meal", {})), ("пищ", ("meal", {})), ("питан", ("meal", {})), ("блюд", ("meal", {})),
          ("завтрак", ("meal", {})), ("обед", ("meal", {})), ("ужин", ("meal", {})), ("перекус", ("meal", {})),
          ("калор", ("meal", {})), ("еду", ("meal", {})), ("еда", ("meal", {})),
          ("вод", ("metric", {"metric": "water"})), ("вес", ("metric", {"metric": "weight"})),
          ("шаг", ("metric", {"metric": "steps"})), ("сон", ("metric", {"metric": "sleep"})),
          ("сна", ("metric", {"metric": "sleep"})), ("привычк", ("metric", {})), ("показател", ("metric", {})),
          ("задач", ("task", {})), ("напоминан", ("task", {})), ("дело", ("task", {})), ("дела", ("task", {})),
          ("заметк", ("note", {})), ("показани", ("meter", {})), ("пробег", ("garage", {})),
          ("накоплен", ("savings", {})), ("отложен", ("savings", {})), ("документ", ("paper", {}))]


def parse_command(cmd: str) -> dict | None:
    """Быстро узнаёт простые команды: «отмени», «отмени последнюю трату», «удали последнюю еду», «/history».
    Сложные фразы («удали кофе, я его не пил») сюда не попадают — их разбирает ИИ (intent undo)."""
    if _LIST_CMD.match(cmd):
        return {"mode": "list"}
    m = _UNDO_VERB.match(cmd)
    if not m:
        return None
    rest = re.findall(r"\w+", m.group(1))
    found: dict = {"what": "any"}
    for w in rest:
        hit = next((v for k, v in _KINDS if w.startswith(k) and (len(k) > 3 or w in (k, k + "у", k + "а", k + "и", k + "ы"))), None)
        if hit:
            if found["what"] not in ("any", hit[0]):
                return None                   # несколько разных типов — пусть разбирается ИИ
            found["what"] = hit[0]
            found.update(hit[1])
            continue
        if any(w == f if len(f) <= 3 else w.startswith(f) for f in _FILLER):
            continue
        return None                           # незнакомое слово: «отмени лимит», «удали кофе» → ИИ
    if not rest and cmd.startswith(("удали", "убери", "убрать", "сотри", "стереть")):
        return None                           # «удали» без уточнения — непонятно что; пусть спросит ИИ
    return {"mode": "last", **found}
