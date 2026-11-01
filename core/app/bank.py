"""Импорт банковских выписок и сверка с записанными тратами.

CSV (Т-Банк, Альфа, ВТБ и др.) разбирается здесь же; если столбцы не узнаны, их определяет ИИ.
PDF (например, Сбер) читает Claude целиком.
Каждая операция сверяется с уже записанными: та же сумма (±1 ₽), тот же тип, дата ±3 дня;
чек, разложенный по категориям, сверяется по общей сумме.
Кнопки: i:<import_id>:all | i:<import_id>:none | i:<import_id>:list
"""
import base64
import csv
import io
import logging
import re
from datetime import date, timedelta
from decimal import Decimal

from . import ai, db, finance
from .config import get_settings
from .prompts import BANK_CATEGORIZE, BANK_COLUMNS, BANK_PDF
from .schemas import Button, Reply
from .users import User
from .util import esc, plural

log = logging.getLogger("bank")
MAX_ROWS = 500

# ---------- Разбор CSV ----------

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})|(\d{1,2})[./](\d{1,2})[./](\d{2,4})")


def _date(v: str) -> date | None:
    m = _DATE_RE.search(v or "")
    if not m:
        return None
    try:
        if m.group(1):
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        y = int(m.group(6))
        return date(y + 2000 if y < 100 else y, int(m.group(5)), int(m.group(4)))
    except ValueError:
        return None


def _amount(v: str) -> Decimal | None:
    s = (v or "").replace(" ", "").replace(" ", "").replace("−", "-").replace("–", "-")
    s = re.sub(r"[^\d,.\-+]", "", s)
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except Exception:
        return None


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _rows(text: str) -> list[list[str]]:
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
    except csv.Error:
        class dialect(csv.excel):   # noqa: N801
            delimiter = ";" if sample.count(";") > sample.count(",") else ","
    return [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]


def _pick(header: list[str], prefer: list[str], avoid: tuple = ()) -> int | None:
    h = [c.lower().replace("ё", "е") for c in header]
    for p in prefer:
        for i, c in enumerate(h):
            if c == p:
                return i
    for p in prefer:
        for i, c in enumerate(h):
            if p in c and not any(a in c for a in avoid):
                return i
    return None


def guess_columns(rows: list[list[str]]) -> dict | None:
    """Ищет строку заголовка и нужные столбцы по названиям."""
    for hi, header in enumerate(rows[:20]):
        low = " ".join(header).lower()
        if not (("дата" in low or "date" in low) and ("сумм" in low or "amount" in low or "расход" in low or "списан" in low)):
            continue
        cols = {
            "header_row": hi,
            "date": _pick(header, ["дата операции", "дата транзакции", "дата", "date"], ("платеж", "проводк", "списан")),
            "amount": _pick(header, ["сумма операции", "сумма в валюте счета", "сумма в валюте карты", "сумма платежа",
                                     "сумма", "amount"], ("кэшбэк", "кешбэк", "бонус", "округл", "комисс")),
            "expense": _pick(header, ["расход", "списание", "дебет"], ("дата",)),
            "income": _pick(header, ["приход", "поступление", "зачисление", "кредит"], ("дата",)),
            "description": _pick(header, ["описание операции", "описание", "назначение платежа", "назначение",
                                          "контрагент", "наименование", "merchant", "description"]),
            "category": _pick(header, ["категория", "category"]),
            "status": _pick(header, ["статус", "status"]),
            "type": _pick(header, ["тип операции", "тип", "type"]),
        }
        if cols["date"] is None or (cols["amount"] is None and cols["expense"] is None):
            continue
        if cols["expense"] is not None and cols["income"] is not None:
            cols["amount"] = None
        return cols
    return None


_INCOME_WORDS = ("пополн", "зачисл", "поступ", "приход", "возврат", "кредит", "income", "credit")
_BAD_STATUS = ("failed", "отклон", "отмен", "declin", "rejected")


def parse_csv(rows: list[list[str]], cols: dict) -> list[dict]:
    out = []
    g = lambda r, k: r[cols[k]] if cols.get(k) is not None and cols[k] < len(r) else ""
    for r in rows[cols["header_row"] + 1:]:
        d = _date(g(r, "date"))
        if not d:
            continue
        if any(b in g(r, "status").lower() for b in _BAD_STATUS):
            continue
        if cols.get("amount") is not None:
            a = _amount(g(r, "amount"))
            if a is None or a == 0:
                continue
            kind = "income" if a > 0 else "expense"
            t = g(r, "type").lower()
            if t and a > 0:   # суммы без знака, тип в отдельном столбце
                kind = "income" if any(w in t for w in _INCOME_WORDS) else "expense"
            a = abs(a)
        else:
            e, i = _amount(g(r, "expense")) or 0, _amount(g(r, "income")) or 0
            if not e and not i:
                continue
            kind, a = ("expense", abs(e)) if e else ("income", abs(i))
        out.append({"date": d, "kind": kind, "amount": a, "description": g(r, "description")[:300],
                    "bank_category": g(r, "category")[:100] or None})
    return out


async def _ai_columns(rows: list[list[str]]) -> dict | None:
    sample = "\n".join(f"{i}: " + " | ".join(r[:20]) for i, r in enumerate(rows[:12]))
    data = await ai.complete_json(BANK_COLUMNS, sample, max_tokens=500)
    if data.get("date") is None or (data.get("amount") is None and data.get("expense") is None):
        return None
    return {k: data.get(k) for k in ("header_row", "date", "amount", "expense", "income", "description",
                                     "category", "status", "type")} | {"header_row": int(data.get("header_row") or 0)}


def _bank_name(filename: str, text: str) -> str | None:
    low = (filename + " " + text[:3000]).lower()
    for key, name in (("tinkoff", "Т-Банк"), ("т-банк", "Т-Банк"), ("тинькофф", "Т-Банк"), ("сбер", "Сбер"),
                      ("sber", "Сбер"), ("альфа", "Альфа-Банк"), ("alfa", "Альфа-Банк"), ("втб", "ВТБ"), ("vtb", "ВТБ"),
                      ("озон", "Ozon Банк"), ("ozon", "Ozon Банк"), ("яндекс", "Яндекс Банк"), ("газпром", "Газпромбанк")):
        if key in low:
            return name
    return None


async def read_file(data: bytes, filename: str, mime: str) -> tuple[list[dict], str | None]:
    name = (filename or "").lower()
    if name.endswith(".pdf") or mime == "application/pdf":
        res = await ai.complete_document(BANK_PDF.format(year=date.today().year),
                                         "Выпиши все операции из выписки.", base64.b64encode(data).decode())
        ops = []
        for o in res.get("operations") or []:
            d, a = _date(str(o.get("date") or "")), _amount(str(o.get("amount") or ""))
            if d and a:
                ops.append({"date": d, "kind": "income" if o.get("kind") == "income" else "expense", "amount": abs(a),
                            "description": str(o.get("description") or "")[:300], "bank_category": o.get("category")})
        return ops, res.get("bank")
    if name.endswith((".xlsx", ".xls")):
        raise ValueError("Файлы Excel пока не разбираю. Сохраните выписку как CSV или пришлите PDF.")
    text = _decode(data)
    rows = _rows(text)
    cols = guess_columns(rows) or await _ai_columns(rows)
    if not cols:
        raise ValueError("Не нашёл в файле столбцы с датой и суммой.")
    return parse_csv(rows, cols), _bank_name(filename, text)


# ---------- Сверка и сохранение ----------

async def _match(family_id: int, ops: list[dict]) -> None:
    """Помечает op['tx'] = id уже записанной траты/дохода (или группы чека)."""
    if not ops:
        return
    a, b = min(o["date"] for o in ops) - timedelta(days=3), max(o["date"] for o in ops) + timedelta(days=3)
    rows = await db.get_pool().fetch(
        "SELECT min(id) AS id, kind, sum(amount) AS amount, min(occurred_on) AS d FROM transactions"
        " WHERE family_id=$1 AND deleted_at IS NULL AND occurred_on BETWEEN $2 AND $3"
        " GROUP BY kind, coalesce(group_id::text, id::text)", family_id, a, b)
    free = [dict(r) for r in rows]
    for o in sorted(ops, key=lambda x: x["date"]):
        best = None
        for c in free:
            if c["kind"] == o["kind"] and abs(Decimal(str(c["amount"])) - o["amount"]) <= 1 \
                    and abs((c["d"] - o["date"]).days) <= 3:
                if best is None or abs((c["d"] - o["date"]).days) < abs((best["d"] - o["date"]).days):
                    best = c
        if best:
            o["tx"] = best["id"]
            free.remove(best)


async def _categorize(ops: list[dict]) -> None:
    exp = ", ".join(await finance.category_names("expense"))
    inc = ", ".join(await finance.category_names("income"))
    for start in range(0, len(ops), 80):
        chunk = ops[start:start + 80]
        lines = "\n".join(f"{i}|{o['kind']}|{o['amount']}|{o['description']}|{o.get('bank_category') or ''}"
                          for i, o in enumerate(chunk))
        try:
            res = await ai.complete_json(BANK_CATEGORIZE.format(expense_cats=exp, income_cats=inc), lines, max_tokens=4000)
        except ai.AIError as e:
            log.warning("Категории для выписки: %s", e)
            continue
        for r in res.get("rows") or []:
            try:
                o = chunk[int(r.get("i"))]
            except (TypeError, ValueError, IndexError):
                continue
            o["category"] = r.get("category")
            o["transfer"] = bool(r.get("transfer"))


async def import_file(user: User, data: bytes, filename: str, mime: str) -> tuple[int | None, Reply]:
    try:
        ops, bank = await read_file(data, filename, mime)
    except ValueError as e:
        return None, Reply(text=f"🏦 {esc(str(e))}")
    if not ops:
        return None, Reply(text="🏦 В файле не нашлось операций. Пришлите выписку в CSV или PDF.")
    truncated = len(ops) > MAX_ROWS
    ops = ops[:MAX_ROWS]
    await _match(user.family_id, ops)
    new = [o for o in ops if not o.get("tx")]
    await _categorize(new)
    pool = db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        imp = await conn.fetchval(
            "INSERT INTO bank_imports (family_id, user_id, filename, bank, date_from, date_to) VALUES ($1,$2,$3,$4,$5,$6)"
            " RETURNING id", user.family_id, user.id, (filename or "")[:200], bank,
            min(o["date"] for o in ops), max(o["date"] for o in ops))
        for o in ops:
            await conn.execute(
                "INSERT INTO bank_rows (import_id, op_date, kind, amount, description, bank_category, category,"
                " is_transfer, status, transaction_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                imp, o["date"], o["kind"], o["amount"], o["description"] or None, o.get("bank_category"),
                o.get("category"), bool(o.get("transfer")), "matched" if o.get("tx") else "new", o.get("tx"))
        await db.emit(conn, "bank.imported", {"import_id": imp, "rows": len(ops), "new": len(new)})
    return imp, await summary(user, imp, truncated)


async def rows_of(family_id: int, imp: int) -> tuple[dict | None, list[dict]]:
    pool = db.get_pool()
    head = await pool.fetchrow("SELECT * FROM bank_imports WHERE id=$1 AND family_id=$2", imp, family_id)
    if not head:
        return None, []
    rows = await pool.fetch("SELECT * FROM bank_rows WHERE import_id=$1 ORDER BY op_date, id", imp)
    return dict(head), [dict(r) for r in rows]


def _row_line(r: dict) -> str:
    sign = "+" if r["kind"] == "income" else "−"
    cat = f" → {esc(r['category'])}" if r.get("category") else ""
    return f"{r['op_date']:%d.%m} {sign}{finance.money(r['amount'])} {esc((r['description'] or '')[:40])}{cat}"


async def summary(user: User, imp: int, truncated: bool = False) -> Reply:
    head, rows = await rows_of(user.family_id, imp)
    if not head:
        return Reply(text="Выписка не найдена.")
    new = [r for r in rows if r["status"] == "new"]
    real = [r for r in new if not r["is_transfer"]]
    transfers = len(new) - len(real)
    exp = sum((r["amount"] for r in real if r["kind"] == "expense"), Decimal(0))
    inc = sum((r["amount"] for r in real if r["kind"] == "income"), Decimal(0))
    period = f"{head['date_from']:%d.%m}–{head['date_to']:%d.%m.%Y}"
    text = (f"🏦 <b>Выписка{(' ' + esc(head['bank'])) if head['bank'] else ''}</b>, {period}\n"
            f"Операций: {len(rows)} · уже записаны: {sum(r['status'] == 'matched' for r in rows)} · "
            f"новых: <b>{len(real)}</b>" + (f" (−{finance.money(exp)}" + (f", +{finance.money(inc)}" if inc else "") + ")" if real else ""))
    if transfers:
        text += f"\nПереводы между своими счетами: {transfers} — их не добавляю"
    added = sum(r["status"] == "added" for r in rows)
    if added:
        text += f"\nУже добавлено из этой выписки: {added}"
    if truncated:
        text += f"\n⚠️ В файле больше {MAX_ROWS} операций, разобрал первые {MAX_ROWS}."
    if real:
        text += "\n\n<b>Новые:</b>\n" + "\n".join(_row_line(r) for r in real[:12])
        if len(real) > 12:
            text += f"\n… и ещё {len(real) - 12}"
    buttons = []
    if real:
        buttons.append([Button(text=f"➕ Добавить все {len(real)}", action=f"i:{imp}:all")])
        url = get_settings().public_url
        if url:
            buttons.append([Button(text="🌐 Выбрать в панели", url=f"{url.rstrip('/')}/web/#import-{imp}")])
        buttons.append([Button(text="✖ Ничего не добавлять", action=f"i:{imp}:none")])
    elif rows:
        text += "\n\n✅ Всё из выписки уже записано."
    return Reply(text=text, buttons=buttons)


async def add_rows(user: User, imp: int, picks: list[dict] | None = None) -> int:
    """Добавляет новые операции. picks = [{id, category}] (из панели) или None = все, кроме переводов."""
    head, rows = await rows_of(user.family_id, imp)
    if not head:
        return 0
    by_id = {r["id"]: r for r in rows if r["status"] == "new"}
    if picks is None:
        chosen = [(r, r["category"]) for r in by_id.values() if not r["is_transfer"]]
    else:
        chosen = [(by_id[p["id"]], p.get("category") or by_id[p["id"]]["category"]) for p in picks if p.get("id") in by_id]
    if not chosen:
        return 0
    items = [{"kind": r["kind"], "amount": float(r["amount"]), "category": cat,
              "merchant": (r["description"] or "")[:80] or None, "date": r["op_date"].isoformat()} for r, cat in chosen]
    saved = await finance.save(user, items, source="bank", raw={"bank_import": imp})
    async with db.get_pool().acquire() as conn, conn.transaction():
        for (r, _), t in zip(chosen, saved):
            await conn.execute("UPDATE bank_rows SET status='added', transaction_id=$2 WHERE id=$1", r["id"], t["id"])
    return len(saved)


async def skip_rest(user: User, imp: int) -> int:
    head, _ = await rows_of(user.family_id, imp)
    if not head:
        return 0
    n = await db.get_pool().execute("UPDATE bank_rows SET status='skipped' WHERE import_id=$1 AND status='new'", imp)
    return int(n.split()[-1])


async def handle_action(user: User, parts: list[str]) -> Reply:
    imp, op = int(parts[1]), parts[2]
    if op == "all":
        n = await add_rows(user, imp)
        r = await summary(user, imp)
        r.text = (f"✅ Добавил {plural(n, 'операцию', 'операции', 'операций')} из выписки.\n"
                  "Отменить любую можно в /history или во вкладке «История» панели.\n\n") + r.text
        return r
    if op == "none":
        n = await skip_rest(user, imp)
        return Reply(text=f"Хорошо, ничего не добавляю (пропущено: {n}).")
    if op == "list":
        return await summary(user, imp)
    return Reply(text="Неизвестное действие.")
