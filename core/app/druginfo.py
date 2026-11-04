"""Краткая справка по лекарству (версия 1.10.6): описание, правила приёма, побочные эффекты, ограничения.

Откуда текст. Сейчас — справка ИИ по названию препарата (источник `ai`). Она всегда помечается: «справка ИИ,
не заменяет инструкцию в упаковке». Для малоизвестных препаратов ИИ отвечает «данных нет», а не выдумывает.
Место для официального текста (Государственный реестр лекарственных средств) оставлено в `official()`:
когда доступ с сервера к реестру проверен, функция вернёт текст инструкции, ИИ только сожмёт его до тех же четырёх
блоков, а источник станет `grls`. Панель и бот при этом менять не нужно.

Чего ассистент не делает: не называет дозы и схемы (мг, число таблеток), не подбирает лекарства и не оценивает
назначение врача. Дозы вводит пользователь сам; в справке — «смотрите в инструкции в упаковке и у врача».
Наружу уходит только название препарата, без сведений о том, кто его принимает.

Кэш — в `app_state`, ключ `drug:<название>`: {at, source, card}. Срок жизни полгода, повторные запросы бесплатны.
Лимит новых запросов к ИИ — DAILY_LIMIT на человека в сутки (кэш не считается).
"""
import logging
import re
import time

from . import ai, release
from .schemas import Reply
from .users import User
from .util import esc

log = logging.getLogger("druginfo")

TTL = 180 * 24 * 3600
DAILY_LIMIT = 30
NOTE = "Справка ИИ для ознакомления, не заменяет инструкцию в упаковке и совет врача. Дозы смотрите в инструкции и у врача."
_used: dict[tuple[int, str], int] = {}

SYSTEM = """Ты помощник-справочник по лекарствам для семейного приложения. Пользователь называет лекарство (торговое
название или действующее вещество). Дай КРАТКУЮ справку на русском простым языком по общепринятым сведениям из
официальных инструкций по применению. Верни ОДИН JSON-объект без пояснений:
{"found": true, "name": "название как принято", "active": "действующее вещество или null",
 "about": "1–2 предложения: что это за препарат и от чего применяют",
 "how": ["правила приёма: с едой или натощак, чем запивать, время суток, можно ли разжёвывать или делить, сколько обычно длится самолечение и когда надо к врачу"],
 "side": ["самые частые и самые важные побочные эффекты, до 6 пунктов, коротко"],
 "restrict": ["противопоказания и ограничения: аллергия, беременность и кормление, возраст детей, болезни почек, печени, желудка, сочетание с алкоголем и другими лекарствами; до 7 пунктов"]}
Строгие правила:
- НЕ указывай дозы и схемы (миллиграммы, число таблеток, кратность в сутки) и не советуй, сколько принимать. Если нужно, напиши «дозу смотрите в инструкции в упаковке и у врача».
- Если препарат тебе не известен, название неоднозначно или ты не уверен в сведениях, верни {"found": false} и больше ничего. Ничего не выдумывай.
- Если это не лекарство, не витамин и не БАД (например, обычная еда), верни {"found": false}.
- Пункты короткие, до 140 символов каждый."""

_MAX_LIST = {"how": 5, "side": 6, "restrict": 7}


def normalize(name) -> str:
    """Название для кэша и запроса: без регистра, «ё», лишних знаков. Пусто, если название не годится."""
    s = re.sub(r"[^\w\s\-]|_", " ", str(name or "").lower().replace("ё", "е"))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:60] if len(s) >= 2 and re.search(r"[a-zа-я]", s) else ""


def clean(v, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:limit]


def build_card(j: dict, source: str = "ai") -> dict | None:
    """Ответ модели → карточка. None, если препарат не найден или ответ пустой."""
    if not isinstance(j, dict) or not j.get("found") or not clean(j.get("name")):
        return None
    card = {"name": clean(j.get("name"), 80), "active": clean(j.get("active"), 120) or None,
            "about": clean(j.get("about"), 400), "source": source}
    for key, n in _MAX_LIST.items():
        raw = j.get(key)
        raw = raw if isinstance(raw, list) else [raw] if raw else []
        card[key] = [clean(x, 160) for x in raw if clean(x, 160)][:n]
    if not card["about"] and not card["side"] and not card["restrict"]:
        return None
    return card


async def official(norm: str) -> str | None:
    """Официальный текст инструкции (ГРЛС). Пока не подключён: возвращает None, работает справка ИИ."""
    return None


def _spend(user: User) -> bool:
    key = (user.id, time.strftime("%Y-%m-%d"))
    for k in [k for k in _used if k[1] != key[1]]:
        _used.pop(k, None)
    if _used.get(key, 0) >= DAILY_LIMIT:
        return False
    _used[key] = _used.get(key, 0) + 1
    return True


async def get_card(user: User, name) -> dict:
    """Карточка препарата: {"ok": True, "card": {...}, "cached": bool} либо {"ok": False, "reason": "..."}.
    reason: bad_name | not_found | limit | error."""
    norm = normalize(name)
    if not norm:
        return {"ok": False, "reason": "bad_name"}
    key = f"drug:{norm}"
    hit = await release.get_state(key)
    if hit.get("card") and time.time() - float(hit.get("at") or 0) < TTL:
        return {"ok": True, "card": hit["card"], "cached": True}
    if hit.get("missing") and time.time() - float(hit.get("at") or 0) < 7 * 24 * 3600:
        return {"ok": False, "reason": "not_found"}
    if not _spend(user):
        return {"ok": False, "reason": "limit"}
    try:
        text = await official(norm)
        if text:
            j = await ai.complete_json(SYSTEM + "\nНиже официальный текст инструкции; используй только его.", f"{norm}\n\n{text[:12000]}", max_tokens=1200)
            source = "grls"
        else:
            j = await ai.complete_json(SYSTEM, norm, max_tokens=1200)
            source = "ai"
    except Exception as e:  # noqa: BLE001
        log.warning("справка по %r не получена: %s", norm, e)
        return {"ok": False, "reason": "error"}
    card = build_card(j, source)
    await release.set_state(key, {"at": time.time(), "card": card} if card else {"at": time.time(), "missing": True})
    return {"ok": True, "card": card, "cached": False} if card else {"ok": False, "reason": "not_found"}


def _bullets(items: list[str]) -> str:
    return "\n".join(f"• {esc(x)}" for x in items)


def card_text(card: dict) -> str:
    """Карточка в виде сообщения Telegram (HTML)."""
    head = f"💊 <b>{esc(card['name'])}</b>" + (f"\n<i>действующее вещество: {esc(card['active'])}</i>" if card.get("active") else "")
    parts = [head]
    if card.get("about"):
        parts.append(esc(card["about"]))
    if card.get("how"):
        parts.append("<b>Как принимать</b>\n" + _bullets(card["how"]))
    if card.get("side"):
        parts.append("<b>Возможные побочные эффекты</b>\n" + _bullets(card["side"]))
    if card.get("restrict"):
        parts.append("<b>Ограничения и противопоказания</b>\n" + _bullets(card["restrict"]))
    parts.append("<i>" + ("Текст по официальной инструкции. " if card.get("source") == "grls" else "") + esc(NOTE) + "</i>")
    return "\n\n".join(parts)


FAIL_TEXT = {
    "bad_name": "Напишите название лекарства, например: <code>/drug нурофен</code>",
    "not_found": "Не нашёл надёжных сведений об этом препарате, поэтому ничего не придумываю. Проверьте написание или откройте инструкцию в упаковке.",
    "limit": "На сегодня лимит новых справок исчерпан (повторные запросы к уже найденным препаратам работают). Попробуйте завтра.",
    "error": "Справка сейчас недоступна (не отвечает ИИ). Попробуйте позже.",
}


async def lookup(user: User, name) -> Reply:
    """Ответ для бота: карточка или понятное объяснение, почему её нет."""
    r = await get_card(user, name)
    if not r["ok"]:
        return Reply(text=FAIL_TEXT[r["reason"]])
    return Reply(text=card_text(r["card"]))
