"""Карты лояльности (версия 1.8): скидочные и клубные карты магазинов со штрихкодом или QR.

На кассе: «покажи карту Ленты» в боте или вкладка «💳 Карты» в панели — штрихкод на весь экран.
Добавить: «карта Пятёрочки 2775 0123 4567 5», фото карты боту, сканер в панели.
По умолчанию раздел общий: карты видит вся семья.

Банковские карты не храним: номер, похожий на банковскую карту, отклоняется.
Кнопки: c:<id>:show | c:<id>:del | c:0:list
"""
import re

from . import db, scope
from .config import get_settings
from .schemas import Button, Reply
from .users import User
from .util import esc

FORMATS = ("ean13", "ean8", "code128", "qr", "text")
FORMAT_NAMES = {"ean13": "штрихкод EAN-13", "ean8": "штрихкод EAN-8", "code128": "штрихкод", "qr": "QR-код",
                "text": "только номер"}
MAX_NUMBER = 300


class CardError(ValueError):
    pass


def _luhn(d: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(d)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def looks_like_bank_card(number: str) -> bool:
    d = re.sub(r"[\s-]", "", number or "")
    return d.isdigit() and 16 <= len(d) <= 19 and d[0] in "2456" and _luhn(d)


def _ean_ok(d: str) -> bool:
    body, check = d[:-1], int(d[-1])
    # справа налево: цифра рядом с контрольной — с весом 3, дальше 1, 3, 1… (так и для EAN-13, и для EAN-8)
    s = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(body)))
    return (10 - s % 10) % 10 == check


def clean(number: str | None, fmt: str | None = None) -> tuple[str, str]:
    """Нормализует номер и выбирает формат. Ошибки — CardError с понятным текстом."""
    raw = (number or "").strip()
    if not raw:
        raise CardError("Не вижу номера карты.")
    if len(raw) > MAX_NUMBER:
        raise CardError("Слишком длинный код.")
    if looks_like_bank_card(raw):
        raise CardError("Похоже на номер банковской карты — такие номера я не храню. "
                        "Здесь только скидочные и клубные карты магазинов.")
    fmt = fmt if fmt in FORMATS else None
    digits = re.sub(r"[\s-]", "", raw)
    if fmt == "qr":
        return raw, "qr"
    if fmt == "text":
        return raw, "text"
    if digits.isdigit() and len(digits) in (8, 13) and _ean_ok(digits) and fmt in (None, "ean13", "ean8"):
        return digits, "ean13" if len(digits) == 13 else "ean8"
    if fmt in ("ean13", "ean8"):
        fmt = None                      # контрольная цифра не сошлась — печатаем как обычный штрихкод
    compact = digits if digits.isdigit() else raw
    if all(32 <= ord(c) < 127 for c in compact) and len(compact) <= 48:
        return compact, "code128"
    return raw, "qr"


async def cards(family_id: int, card_id: int | None = None) -> list[dict]:
    rows = await db.get_pool().fetch(
        "SELECT c.*, u.name AS user_name FROM loyalty_cards c JOIN users u ON u.id=c.user_id"
        f" WHERE c.family_id=$1 AND c.deleted_at IS NULL AND {scope.cond('cards', 'c.user_id')}"
        " AND ($2::bigint IS NULL OR c.id=$2)"
        " ORDER BY c.used_count DESC, lower(c.store)", family_id, card_id)
    return [dict(r) for r in rows]


def _norm(s: str | None) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[«»\"'“”]", "", s)
    s = re.sub(r"\b(карт[аыуеой]*|скидочн\w*|клубн\w*|магазин\w*|сеть|покажи|открой|дай)\b", " ", s)
    return " ".join(s.split())


def _stem(w: str) -> str:
    return w[:max(3, len(w) - 2)]


async def find(user: User, query: str | None) -> list[dict]:
    """Карты по названию магазина: «Ленты» ≈ «Лента», «пятерочки» ≈ «Пятёрочка»."""
    cs = await cards(user.family_id)
    q = _norm(query)
    if not q:
        return cs
    words = [_stem(w) for w in q.split()]
    hit = [c for c in cs if all(w in _norm(c["store"]) for w in words)]
    return hit or [c for c in cs if any(w in _norm(c["store"]) for w in words)]


def panel_url(card_id: int | None = None) -> str | None:
    url = get_settings().public_url
    if not url:
        return None
    return url.rstrip("/") + "/web" + (f"#card-{card_id}" if card_id else "#cards")


def _line(c: dict) -> str:
    return f"💳 <b>{esc(c['store'])}</b>" + (f" · {esc(c['note'])}" if c.get("note") else "")


async def add(user: User, data: dict, source: str = "text") -> Reply:
    store = (data.get("store") or "").strip()[:80]
    if not store:
        return Reply(text="Какого магазина карта? Например: <code>карта Пятёрочки 2775 0123 4567 5</code>")
    try:
        number, fmt = clean(data.get("number"), data.get("format"))
    except CardError as e:
        return Reply(text="💳 " + esc(str(e)))
    pool = db.get_pool()
    dup = await pool.fetchrow(
        "SELECT id FROM loyalty_cards WHERE family_id=$1 AND deleted_at IS NULL AND lower(store)=lower($2) AND number=$3",
        user.family_id, store, number)
    if dup:
        return Reply(text=f"💳 Карта «{esc(store)}» с этим номером уже есть.", buttons=_buttons(dup["id"]))
    cid = await pool.fetchval(
        "INSERT INTO loyalty_cards (family_id, user_id, store, number, format, note) VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
        user.family_id, user.id, store, number, fmt, (data.get("note") or None) and str(data["note"])[:120])
    who = "Только вам" if scope.is_personal("cards") else "Карта видна всей семье"
    text = (f"💳 Сохранил карту <b>{esc(store)}</b> ({FORMAT_NAMES[fmt]}): <code>{esc(number)}</code>\n"
            f"{who}. На кассе: «покажи карту {esc(store)}» или вкладка «💳 Карты» в панели.")
    return Reply(text=text, buttons=_buttons(cid, with_undo=True))


def _buttons(cid: int, with_undo: bool = False) -> list[list[Button]]:
    row = []
    url = panel_url(cid)
    if url:
        row.append(Button(text="📲 Штрихкод на весь экран", url=url))
    rows = [row] if row else []
    if with_undo:
        rows.append([Button(text="↩️ Отменить", action=f"u:c.{cid}:del")])
    return rows


async def mark_used(user: User, card_id: int) -> None:
    await db.get_pool().execute(
        "UPDATE loyalty_cards SET used_count=used_count+1, used_at=now() WHERE id=$1 AND family_id=$2",
        card_id, user.family_id)


async def show(user: User, query: str | None) -> Reply:
    found = await find(user, query)
    if not found:
        allc = await cards(user.family_id)
        if not allc:
            return Reply(text="💳 Карт пока нет. Пришлите фото карты или напишите: <code>карта Ленты 1234567890123</code>")
        return Reply(text=f"💳 Карты «{esc(query or '')}» не нашёл. Есть: " + ", ".join(esc(c["store"]) for c in allc[:15]),
                     buttons=[[Button(text=c["store"][:30], action=f"c:{c['id']}:show")] for c in allc[:6]])
    if len(found) > 1 and query:
        return Reply(text="💳 Какую показать?",
                     buttons=[[Button(text=f"{c['store']}"[:30] + (f" · {c['note']}" if c.get('note') else "")[:20],
                                      action=f"c:{c['id']}:show")] for c in found[:8]])
    if len(found) > 1:
        return await list_reply(user)
    return await _show_one(user, found[0])


async def _show_one(user: User, c: dict) -> Reply:
    await mark_used(user, c["id"])
    text = (f"{_line(c)}\nНомер: <code>{esc(c['number'])}</code>\n"
            + ("Нажмите кнопку — откроется штрихкод для кассы." if panel_url() else
               "Продиктуйте номер на кассе или покажите его на экране."))
    return Reply(text=text, buttons=_buttons(c["id"]))


async def list_reply(user: User) -> Reply:
    cs = await cards(user.family_id)
    if not cs:
        return Reply(text="💳 Карт лояльности пока нет.\nПришлите фото карты или напишите: "
                          "<code>карта Пятёрочки 2775 0123 4567 5</code>")
    lines = [f"💳 <b>Карты лояльности</b> ({len(cs)})"] + [_line(c) for c in cs[:30]]
    btns = [[Button(text=c["store"][:30], action=f"c:{c['id']}:show")] for c in cs[:8]]
    url = panel_url()
    if url:
        btns.append([Button(text="📲 Все карты в панели", url=url)])
    return Reply(text="\n".join(lines), buttons=btns)


async def handle_action(user: User, parts: list[str]) -> Reply:
    """c:<id>:show | c:0:list"""
    if parts[2] == "list":
        return await list_reply(user)
    try:
        cid = int(parts[1])
    except ValueError:
        return Reply(text="Кнопка устарела.")
    found = await cards(user.family_id, cid)
    if not found:
        return Reply(text="💳 Этой карты уже нет.")
    if parts[2] == "show":
        return await _show_one(user, found[0])
    return Reply(text="Кнопка устарела.")
