"""Траты из уведомлений и SMS банков (версия 1.9): Сбер, Т-Банк, ВТБ, Альфа-Банк.

Приложение на телефоне присылает текст уведомления или SMS банка. Здесь он:
1) обезличивается — длинные номера убираются, от карты остаются 4 последние цифры, коды и пароли не сохраняются вовсе;
2) разбирается: покупка или поступление, сумма, остаток, карта, магазин;
3) сверяется, чтобы не было дублей: то же уведомление ещё раз, та же операция в push и в SMS,
   та же трата, уже записанная вручную (и наоборот — ручная запись после уведомления убирает автоматическую);
4) записывается в финансы, если человек включил это в настройках (по умолчанию выключено).
Остатки по картам видны в «💰 Финансах».
"""
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from . import bank, db, finance, notify, prefs, users
from .config import get_settings
from .schemas import Reply

log = logging.getLogger("bankfeed")

BANK_NAMES = {"sber": "Сбер", "tbank": "Т-Банк", "vtb": "ВТБ", "alfa": "Альфа-Банк"}
# Приложения банков (у банков бывает несколько приложений и новые имена после удаления из магазинов)
PACKAGES = {
    "sber": ["ru.sberbankmobile", "ru.sberbank.sberbankid", "ru.sberbank.spasibo", "ru.sberbankmobile.sberbank"],
    "tbank": ["com.idamob.tinkoff.android", "ru.tinkoff.mb", "ru.tbank.mb", "com.tbank.mb"],
    "vtb": ["ru.vtb24.mobilebanking.android", "ru.vtb.mobilebanking", "ru.vtb.online"],
    "alfa": ["ru.alfabank.mobile.android", "ru.alfabank.oavdo.amc", "ru.alfabank.mobile"],
}
# Если пакет незнакомый — узнаём банк по названию приложения
LABELS_RE = {"sber": "сбер|sber|сбол", "tbank": "т-банк|тинькофф|t-bank|tbank|tinkoff",
             "vtb": "втб|vtb", "alfa": "альфа|alfa"}
# Отправители SMS (сравнение без регистра)
SENDERS = {"sber": ["900", "sberbank", "sber", "sberbank_online"], "tbank": ["tinkoff", "t-bank", "tbank"],
           "vtb": ["vtb", "vtb24", "vtb_bank"], "alfa": ["alfa-bank", "alfabank", "alfa_bank"]}

_SECRET = re.compile(r"код\b|кода\b|пароль|никому не сообщ|не сообщайте|подтвержд|вход в |войти|логин", re.I)
_DECLINED = re.compile(r"отклон|недостаточно|отказ|не прош|не выполн|заблокир|ошибка", re.I)
_PROMO = re.compile(r"предлага|акци[яи]|успейте|подключите|оформите|одобрен|ставк|до \d+\s*%|розыгрыш|подарок от", re.I)
_INCOME = re.compile(r"зачислен|поступлен|пополнен|перевод[^.]{0,25}?\sот\s|входящий перевод|получен[ы]? перевод|вам перевел|"
                     r"возврат|отмена покупки|зарплат|кэшб[эе]к|кешб[эе]к|начислен", re.I)
_EXPENSE = re.compile(r"покупк|оплат|списан|снят|выдач|перевод|платёж|платеж|исходящ|поездк|абонентск", re.I)
_OWN = re.compile(r"между (?:своими )?сч|сво(?:их|ими|й|его) сч|на свой|со своего|с вклада|на вклад|накопит|копилк", re.I)
_MONEY = re.compile(r"(?<![\d*•·])([-−]?\d{1,3}(?:[   ]\d{3})+|[-−]?\d+)(?:[.,](\d{1,2}))?\s*(?:₽|р\.|р\b|руб|rub|rur)", re.I)
_BALANCE = re.compile(r"(?:баланс|доступно|остаток|доступный остаток|ост\.)[:\s]*", re.I)
_CARD = re.compile(r"(?:\*+|•+\s?|·+\s?|\.\.|x{2,}|mir-?|ecmc|visa|maestro|mc|карт[аеыу]?\s*[*•·]*\s*|"
                   r"сч[её]т[а-я]*\s*[*•·]*\s*)(\d{4})\b", re.I)
_LONG_NUM = re.compile(r"\d(?:[  -]?\d){11,}")
_PHONE = re.compile(r"\+7[\d\s()\-]{9,}\d")


def detect_bank(app: str = "", label: str = "", sender: str = "") -> str | None:
    app, label, sender = (app or "").lower(), (label or "").lower(), (sender or "").lower().strip()
    for b, pk in PACKAGES.items():
        if app in pk:
            return b
    for b, snd in SENDERS.items():
        if sender and sender in snd:
            return b
    for b, rx in LABELS_RE.items():
        if label and re.search(rx, label):
            return b
    return None


def sanitize(text: str) -> str:
    """Убирает полные номера карт и счетов (оставляет 4 последние цифры) и номера телефонов."""
    t = _LONG_NUM.sub(lambda m: "••" + re.sub(r"\D", "", m.group())[-4:], text or "")
    t = _PHONE.sub("+7•••", t)
    return re.sub(r"\s+", " ", t).strip()[:500]


def _num(m: re.Match) -> Decimal | None:
    whole = re.sub(r"[   ]", "", m.group(1)).replace("−", "-")
    try:
        return Decimal(whole + "." + (m.group(2) or "0"))
    except InvalidOperation:
        return None


def parse(text: str) -> dict | None:
    """Разбор текста банка. None — это не операция (код, реклама, отказ) и сохранять ничего не надо."""
    if not text or _SECRET.search(text) or _PROMO.search(text) or _DECLINED.search(text):
        return None
    bal_span, balance = None, None
    for bm in _BALANCE.finditer(text):
        mm = _MONEY.match(text, bm.end())
        if mm:
            bal_span, balance = (bm.start(), mm.end()), _num(mm)
            break
    amount, a_end = None, None
    for mm in _MONEY.finditer(text):
        if bal_span and bal_span[0] <= mm.start() < bal_span[1]:
            continue
        amount, a_end = _num(mm), mm.end()
        break
    kind = "income" if _INCOME.search(text) else "expense" if _EXPENSE.search(text) else None
    if amount is not None:
        amount = abs(amount)
    if not kind or not amount:
        return {"kind": None, "amount": None, "balance": balance, "card_tail": _tail(text), "merchant": None,
                "own": False} if balance is not None else {"kind": None, "unparsed": True}
    merchant = None
    if a_end:
        rest = text[a_end:bal_span[0] if bal_span and bal_span[0] > a_end else len(text)]
        rest = _CARD.sub(" ", rest)
        rest = re.sub(r"\b\d{1,2}[:.]\d{2}(?:[:.]\d{2,4})?\b|\b\d{1,2}\.\d{1,2}\.\d{2,4}\b", " ", rest)
        rest = re.sub(r"^[\s.,;:\-—]*(?:в|во|на)\s+", "", rest.strip(" .,;:-—"), flags=re.I)
        rest = re.sub(r"[«»\"]", "", rest)
        rest = re.split(r"\.\s|,\s(?=[А-ЯA-Z]{2,}\b)|\s{2,}", rest)[0].strip(" .,;:-—")
        merchant = rest[:60] or None
    return {"kind": kind, "amount": amount, "balance": balance, "card_tail": _tail(text), "merchant": merchant,
            "own": bool(_OWN.search(text))}


def _tail(text: str) -> str | None:
    m = _CARD.search(text or "")
    return m.group(1) if m else None


def _at(v) -> datetime:
    """Время уведомления: миллисекунды с 1970 или ISO; иначе — сейчас. Будущее и очень старое не принимаем."""
    now = datetime.now(timezone.utc)
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():
            t = datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc)
        else:
            t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return now
    return t if now - timedelta(days=30) < t <= now + timedelta(minutes=5) else now


async def ingest(device: dict, items: list) -> dict:
    """Пачка уведомлений/SMS с телефона → итог по статусам."""
    user = await users.get(device["user_id"])
    s = await prefs.get(user.id)
    stats: dict[str, int] = {}
    if not s.get("bank_auto"):
        return {"off": True, "stats": stats}
    for it in (items or [])[:100]:
        if not isinstance(it, dict):
            continue
        try:
            st = await _one(user, device, it)
        except Exception:
            log.exception("Уведомление банка")
            st = "error"
        stats[st] = stats.get(st, 0) + 1
    return {"off": False, "stats": stats}


async def _one(user: users.User, device: dict, it: dict) -> str:
    raw = " ".join(str(it.get(k) or "") for k in ("title", "text")).strip()
    channel = "sms" if it.get("channel") == "sms" else "push"
    b = detect_bank(it.get("app"), it.get("label"), it.get("sender") if channel == "sms" else "")
    if not b or not raw:
        return "ignored"
    p = parse(raw)
    if p is None:
        return "ignored"            # коды, реклама и отказы не храним вовсе
    text = sanitize(raw)
    at = _at(it.get("at"))
    h = hashlib.sha256(f"{b}|{text}|{int(at.timestamp() // 60)}".encode()).hexdigest()
    pool = db.get_pool()
    if await pool.fetchval("SELECT 1 FROM bank_events WHERE user_id=$1 AND text_hash=$2", user.id, h):
        return "repeat"
    status, tx_id = "unparsed", None
    if p.get("kind"):
        dup = await pool.fetchval(
            "SELECT id FROM bank_events WHERE family_id=$1 AND bank=$2 AND kind=$3 AND amount=$4"
            " AND (card_tail IS NULL OR $5::text IS NULL OR card_tail=$5)"
            " AND abs(extract(epoch FROM occurred_at - $6::timestamptz)) <= 900 LIMIT 1",
            user.family_id, b, p["kind"], p["amount"], p["card_tail"], at)
        if dup:
            status = "duplicate"
        elif p["own"]:
            status = "transfer"
        else:
            status, tx_id = await _record(user, b, p, at)
    elif p.get("balance") is not None:
        status = "balance"
    await pool.execute(
        "INSERT INTO bank_events (family_id, user_id, device_id, bank, channel, kind, amount, balance, card_tail,"
        " merchant, text, text_hash, occurred_at, status, transaction_id)"
        " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) ON CONFLICT (user_id, text_hash) DO NOTHING",
        user.family_id, user.id, device.get("id"), b, channel, p.get("kind"), p.get("amount"), p.get("balance"),
        p.get("card_tail"), p.get("merchant"), text, h, at, status, tx_id)
    return status


async def _record(user: users.User, b: str, p: dict, at: datetime) -> tuple[str, int | None]:
    pool = db.get_pool()
    day = at.astimezone(get_settings().zone).date()
    # уже записано вручную (или регулярным платежом) — только связываем
    tx = await pool.fetchval(
        "SELECT t.id FROM (SELECT min(id) AS id, min(user_id) AS uid, kind, sum(amount) AS amount, min(occurred_on) AS d"
        "  FROM transactions WHERE family_id=$1 AND deleted_at IS NULL AND source NOT IN ('bank_push', 'bank')"
        "  AND occurred_on BETWEEN $2::date - 1 AND $2::date + 1 GROUP BY kind, coalesce(group_id::text, id::text)) t"
        " WHERE t.kind=$3 AND abs(t.amount - $4) <= 1"
        " AND NOT EXISTS (SELECT 1 FROM bank_events e WHERE e.transaction_id=t.id)"
        " ORDER BY (t.uid=$5) DESC, abs(t.d - $2::date) LIMIT 1",
        user.family_id, day, p["kind"], p["amount"], user.id)
    if tx:
        return "matched", tx
    op = {"kind": p["kind"], "amount": p["amount"], "description": p.get("merchant") or BANK_NAMES[b], "date": day}
    await bank._categorize([op])
    if op.get("transfer"):
        return "transfer", None
    rows = await finance.save(user, [{"kind": p["kind"], "amount": p["amount"], "category": op.get("category"),
                                      "merchant": p.get("merchant"), "date": day.isoformat()}],
                              source="bank_push", raw={"bank": b})
    if not rows:
        return "unparsed", None
    reply = finance.render_saved(rows, header=f"🏦 Из уведомления {BANK_NAMES[b]}"
                                 + (f" (карта ••{p['card_tail']})" if p.get("card_tail") else "") + ":")
    reply.text = reply.text.replace("💾 Записал:\n", "")
    await notify.send(user.id, reply, topic="bank", quiet="defer")
    return "saved", rows[0]["id"]


async def absorb_manual(user: users.User, saved: list[dict]) -> None:
    """Ручная запись после автоматической из уведомления: автоматическую убираем, ручную оставляем.

    Помечает в saved поле bank_dup, чтобы в ответе было видно, что дубль убран."""
    if not saved:
        return
    groups: dict[str, list[dict]] = {}
    for t in saved:
        groups.setdefault(str(t.get("group_id") or t["id"]), []).append(t)
    pool = db.get_pool()
    for rows in groups.values():
        kind = rows[0]["kind"]
        total = sum(Decimal(str(t["amount"])) for t in rows if t["kind"] == kind)
        auto = await pool.fetchval(
            "SELECT id FROM transactions WHERE family_id=$1 AND source='bank_push' AND deleted_at IS NULL"
            " AND kind=$2 AND abs(amount - $3) <= 1 AND occurred_on BETWEEN $4::date - 1 AND $4::date + 1"
            " AND created_at > now() - interval '3 days' ORDER BY (user_id=$5) DESC, id DESC LIMIT 1",
            user.family_id, kind, total, rows[0]["occurred_on"], user.id)
        if not auto:
            continue
        await pool.execute("UPDATE transactions SET deleted_at=now() WHERE id=$1", auto)
        await pool.execute("UPDATE bank_events SET transaction_id=$2, status='matched' WHERE transaction_id=$1",
                           auto, rows[0]["id"])
        rows[0]["bank_dup"] = True


async def overview(user: users.User) -> dict:
    """Для «Финансов»: последние остатки по картам и последние уведомления."""
    pool = db.get_pool()
    bal = await pool.fetch(
        "SELECT DISTINCT ON (e.bank, coalesce(e.card_tail, '')) e.bank, e.card_tail, e.balance, e.occurred_at, u.name AS owner"
        " FROM bank_events e JOIN users u ON u.id=e.user_id WHERE e.family_id=$1 AND e.balance IS NOT NULL"
        " ORDER BY e.bank, coalesce(e.card_tail, ''), e.occurred_at DESC", user.family_id)
    last = await pool.fetch(
        "SELECT e.id, e.bank, e.channel, e.kind, e.amount, e.card_tail, e.merchant, e.status, e.occurred_at,"
        " e.transaction_id, u.name AS owner FROM bank_events e JOIN users u ON u.id=e.user_id"
        " WHERE e.family_id=$1 ORDER BY e.occurred_at DESC, e.id DESC LIMIT 15", user.family_id)
    on = [m.name for m in await users.family_members(user.family_id) if (await prefs.get(m.id)).get("bank_auto")]
    return {"balances": [{**dict(r), "bank_name": BANK_NAMES.get(r["bank"], r["bank"])} for r in bal],
            "events": [{**dict(r), "bank_name": BANK_NAMES.get(r["bank"], r["bank"])} for r in last],
            "enabled_for": on, "me_on": user.name in on}


def dup_note(rows: list[dict]) -> str:
    return "\n🏦 Совпало с уведомлением банка — автоматическую запись убрал, чтобы не было дубля." \
        if any(t.get("bank_dup") for t in rows) else ""

