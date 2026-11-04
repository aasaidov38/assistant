"""Баланс GPTunnel (версия 1.10.2): остаток на счёте, прогноз «на сколько дней хватит», уведомления владельцу.

Работает, только если ИИ подключён через GPTunnel (LLM_BASE_URL на gptunnel.ru). Ключ берётся тот же, что для моделей
(LLM_API_KEY), отдельных ключей не нужно. Запрос: GET https://gptunnel.ru/v1/balance → {"balance": 988.78, "creditLimit": 0}.

Хранение — в app_state, ключ `balance:gptunnel`: {at, tried, data: {balance, credit}, history: [[время, остаток], ...]}.
История нужна, чтобы считать средний расход за сутки; пополнения (остаток вырос) в расход не входят.
Уведомления: ключ `balance:alert` = {level: ok|low|zero, at}. Повтор — раз в сутки, пока остаток не пополнен.
"""
import asyncio
import json
import logging
import time
from urllib.parse import urlparse

from . import db, net, notify, prefs, release
from .config import get_settings
from .schemas import Reply
from .util import esc

log = logging.getLogger("balance")

KEY = "balance:gptunnel"
ALERT = "balance:alert"
TTL = 3600                 # обновляем раз в час
RETRY = 600                # после неудачи — через 10 минут
REPEAT = 24 * 3600         # напоминать о нехватке не чаще раза в сутки
HISTORY_DAYS = 40
DEFAULT_LOW = 300          # рублей
_lock = asyncio.Lock()
_tasks: set = set()


def endpoint() -> str | None:
    """Адрес запроса баланса или None, если ИИ подключён не через GPTunnel / нет ключа."""
    s = get_settings()
    if (s.llm_provider or "").lower() != "openai" or not s.llm_api_key:
        return None
    u = urlparse(s.llm_base_url or "")
    if not u.hostname or not (u.hostname == "gptunnel.ru" or u.hostname.endswith(".gptunnel.ru")):
        return None
    return f"{u.scheme or 'https'}://{u.netloc}/v1/balance"


def enabled() -> bool:
    return endpoint() is not None


def parse(j: dict) -> dict:
    """Ответ GPTunnel → {balance, credit}. Исключение, если остатка в ответе нет."""
    if not isinstance(j, dict) or j.get("balance") is None:
        raise ValueError("в ответе нет поля balance")
    return {"balance": round(float(j["balance"]), 2), "credit": round(float(j.get("creditLimit") or 0), 2)}


def rate_per_day(history: list, now: float | None = None, days: int = 7) -> float | None:
    """Средний расход в рублях за сутки по истории за последние `days` дней. Пополнения не считаются.
    None — данных мало (меньше 6 часов наблюдений) или расхода не было."""
    now = now or time.time()
    pts = sorted((t, b) for t, b in history if t >= now - days * 86400)
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < 6 * 3600:
        return None
    spent = sum(max(0.0, a[1] - b[1]) for a, b in zip(pts, pts[1:]))
    rate = spent * 86400 / (pts[-1][0] - pts[0][0])
    return round(rate, 2) if rate > 0.01 else None


def days_left(balance: float, rate: float | None) -> int | None:
    if not rate or balance <= 0:
        return None
    return int(balance // rate)


def level(balance: float, low: int) -> str:
    return "zero" if balance <= 0 else "low" if balance < low else "ok"


# ---------- хранение и обновление ----------

async def _load(key: str = KEY) -> dict:
    v = await db.get_pool().fetchval("SELECT value FROM app_state WHERE key=$1", key)
    if isinstance(v, str):
        v = json.loads(v or "{}")
    return v or {}


async def _save(rec: dict, key: str = KEY) -> None:
    await db.get_pool().execute(
        "INSERT INTO app_state (key, value, updated_at) VALUES ($1, $2::jsonb, now())"
        " ON CONFLICT (key) DO UPDATE SET value=$2::jsonb, updated_at=now()", key, rec)


async def fetch() -> dict:
    url = endpoint()
    if not url:
        raise RuntimeError("GPTunnel не подключён")
    j = await net.get(url, headers={"Authorization": "Bearer " + get_settings().llm_api_key})
    return parse(j)


async def refresh() -> bool:
    """Запрашивает баланс, дописывает историю и проверяет, не пора ли уведомить. При ошибке прежние данные остаются."""
    if not enabled():
        return False
    async with _lock:
        rec = await _load()
        now = time.time()
        try:
            data = await asyncio.wait_for(fetch(), 30)
        except Exception as e:
            log.warning("Баланс GPTunnel: %s", e)
            rec["tried"] = now
            await _save(rec)
            return False
        hist = [h for h in rec.get("history") or [] if h[0] >= now - HISTORY_DAYS * 86400]
        if not hist or now - hist[-1][0] >= 1800 or abs(hist[-1][1] - data["balance"]) > 0.005:
            hist.append([now, data["balance"]])
        await _save({"at": now, "tried": now, "data": data, "history": hist})
    try:
        await check_alerts(data["balance"])
    except Exception:
        log.exception("Баланс GPTunnel: уведомление")
    return True


def _spawn() -> None:
    t = asyncio.create_task(refresh())
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _due(rec: dict) -> bool:
    now = time.time()
    return now - rec.get("at", 0) >= TTL and now - rec.get("tried", 0) >= RETRY


async def refresh_due() -> None:
    """Фоновая задача (jobs.loop): раз в час обновить баланс."""
    if enabled() and _due(await _load()):
        await refresh()


# ---------- уведомления владельцу ----------

def _money(v: float) -> str:
    return f"{v:,.2f}".replace(",", " ").replace(".", ",").replace(",00", "") + " ₽"


async def check_alerts(balance: float) -> int:
    """Пишет владельцу, когда остаток ниже порога или закончился. Возвращает число отправленных сообщений."""
    st = await _load(ALERT)
    now = time.time()
    sent = 0
    for o in await release._owner():
        low = (await prefs.get(o.id)).get("balance_low", DEFAULT_LOW)
        lv = level(balance, low)
        key = str(o.id)
        prev = (st.get(key) or {})
        if lv == "ok":
            st[key] = {"level": "ok", "at": prev.get("at", 0)}
            continue
        if prev.get("level") == lv and now - prev.get("at", 0) < REPEAT:
            continue
        if lv == "zero":
            text = (f"🚨 <b>Деньги на GPTunnel закончились</b>\nОстаток: {esc(_money(balance))}. "
                    "Пока счёт не пополнен, ассистент может не отвечать на сообщения, где нужен ИИ.\n"
                    "Пополнить: gptunnel.ru → личный кабинет.")
        else:
            rec = await _load()
            d = days_left(balance, rate_per_day(rec.get("history") or []))
            text = (f"💸 <b>На GPTunnel заканчиваются деньги</b>\nОстаток: {esc(_money(balance))} "
                    f"(порог {esc(_money(float(low)))})." + (f"\nПри обычном расходе хватит примерно на {d} дн." if d is not None else "")
                    + "\nПополнить: gptunnel.ru → личный кабинет.")
        ok = await notify.send(o.id, Reply(text=text), topic="balance", urgent=(lv == "zero"))
        # если человек выключил тему уведомлений, отметку не ставим вперёд: включит — получит
        st[key] = {"level": lv, "at": now if ok else prev.get("at", 0)}
        sent += 1 if ok else 0
    await _save(st, ALERT)
    return sent


# ---------- для панели ----------

async def overview(low: int) -> dict:
    """Данные карточки. Если записи ещё нет — один раз запрашиваем сразу, чтобы карточка не была пустой."""
    if not enabled():
        return {"enabled": False}
    rec = await _load()
    if not rec.get("data"):
        try:
            await asyncio.wait_for(refresh(), 10)
        except Exception:
            pass
        rec = await _load()
    elif _due(rec):
        _spawn()                      # просроченное отдаём сразу, а обновляется в фоне
    d = rec.get("data")
    if not d:
        return {"enabled": True, "empty": True, "low": low}
    r = rate_per_day(rec.get("history") or [])
    return {"enabled": True, "empty": False, "balance": d["balance"], "credit": d["credit"], "at": rec.get("at"),
            "stale": time.time() - rec.get("at", 0) > 3 * TTL, "rate_per_day": r, "days_left": days_left(d["balance"], r),
            "low": low, "level": level(d["balance"], low)}
