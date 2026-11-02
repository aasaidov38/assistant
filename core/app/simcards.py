"""Баланс SIM-карт (версия 1.9): телефон раз в несколько часов отправляет USSD-запрос по каждой SIM
и присылает ответ оператора. Здесь из ответа достаётся сумма, а при низком балансе хозяину телефона
приходит напоминание (один раз, пока баланс снова не поднимется выше порога из настроек)."""
import re
from decimal import Decimal, InvalidOperation

from . import db, finance, notify, prefs
from .schemas import Reply
from .util import esc

# Код MCC+MNC сети → оператор и USSD-запрос баланса. Можно поправить здесь — телефон получит новый список сам.
USSD = [
    {"op": "МТС", "mccmnc": ["25001"], "match": "mts|мтс", "code": "*100#"},
    {"op": "МегаФон", "mccmnc": ["25002"], "match": "megafon|мегафон", "code": "*100#"},
    {"op": "Билайн", "mccmnc": ["25099", "25028"], "match": "beeline|билайн", "code": "*102#"},
    {"op": "Т2", "mccmnc": ["25020"], "match": "tele2|теле2|^t2|т2", "code": "*105#"},
    {"op": "Yota", "mccmnc": ["25011"], "match": "yota|йота", "code": "*100#"},
]
_NUM = r"([-−]?\s?\d{1,3}(?:[  ]\d{3})*(?:[.,]\d{1,2})?|[-−]?\d+(?:[.,]\d{1,2})?)"
_AFTER = re.compile(r"(?:баланс|balance|на сч[её]те|остаток|ост\.?)\s*(?:[:=]|составляет|равен)?\s*(минус\s*)?" + _NUM, re.I)
_ANY = re.compile(r"(минус\s*)?" + _NUM + r"\s*(?:₽|р\.|р\b|руб|r\b|rub)", re.I)


def operator(mccmnc: str = "", name: str = "") -> str:
    for o in USSD:
        if mccmnc and mccmnc in o["mccmnc"]:
            return o["op"]
    for o in USSD:
        if name and re.search(o["match"], name, re.I):
            return o["op"]
    return (name or "Оператор").strip()[:30]


def parse_balance(text: str) -> Decimal | None:
    m = _AFTER.search(text or "") or _ANY.search(text or "")
    if not m:
        return None
    raw = re.sub(r"[\s ]", "", m.group(2)).replace("−", "-").replace(",", ".")
    try:
        v = Decimal(raw)
    except InvalidOperation:
        return None
    if m.group(1):
        v = -abs(v)
    return v if abs(v) < 1_000_000 else None


async def ingest(device: dict, items: list) -> list[dict]:
    pool = db.get_pool()
    s = await prefs.get(device["user_id"])
    low = Decimal(str(s.get("sim_low", 100)))
    out = []
    for it in (items or [])[:4]:
        if not isinstance(it, dict):
            continue
        try:
            slot = int(it.get("slot", 0))
        except (TypeError, ValueError):
            continue
        op = operator(str(it.get("mccmnc") or ""), str(it.get("operator") or ""))
        text = str(it.get("text") or "")[:500]
        bal = parse_balance(text) if text else None
        err = None if bal is not None else (str(it.get("error") or "") or "не понял ответ оператора")[:120]
        row = await pool.fetchrow(
            "INSERT INTO sim_cards (device_id, slot, operator, balance, error, checked_at) VALUES ($1,$2,$3,$4,$5,now())"
            " ON CONFLICT (device_id, slot) DO UPDATE SET operator=$3,"
            "  balance=coalesce($4, sim_cards.balance), error=$5, checked_at=now()"
            " RETURNING id, balance, low_sent_at", device["id"], slot, op, bal, err)
        if bal is not None and low > 0:
            if bal < low and not row["low_sent_at"]:
                await notify.send(device["user_id"], Reply(
                    text=f"📶 На {esc(op)} (SIM {slot + 1}, {esc(device['name'])}) осталось <b>{finance.money(bal)}</b>.\n"
                         f"Пора пополнить — порог в настройках: {finance.money(low)}."), topic="sim")
                await pool.execute("UPDATE sim_cards SET low_sent_at=now() WHERE id=$1", row["id"])
            elif bal >= low and row["low_sent_at"]:
                await pool.execute("UPDATE sim_cards SET low_sent_at=NULL WHERE id=$1", row["id"])
        out.append({"slot": slot, "operator": op, "balance": float(bal) if bal is not None else None, "error": err})
    return out
