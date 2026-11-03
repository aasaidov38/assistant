"""Рынок (версия 1.10): курсы валют и крипты, индексы, ключевая ставка и инфляция. Всё справочное, без ключей.

Источники (бесплатные, без регистрации):
  • курсы валют — ЦБ РФ (cbr.ru/scripts/XML_daily.asp), изменение — к предыдущему дню;
  • крипта — CoinGecko, запасной источник — Binance (цена в долларах, рубли считаются по курсу ЦБ);
  • индексы — Мосбиржа (ISS) для IMOEX; S&P 500 и NASDAQ — Stooq, запасной источник — Yahoo Finance;
  • ключевая ставка и инфляция — страницы cbr.ru/hd_base.

Данные кэшируются в app_state: показываем всегда последнее удачное значение, даже если источник недоступен
(тогда на карточке пометка «данные не обновились»). Обновление идёт в фоне, панель не ждёт сети.
"""
import asyncio
import csv
import html as _html
import io
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from xml.etree import ElementTree as ET

from . import db, net
from .util import today

log = logging.getLogger("market")

FX = [("USD", "Доллар США"), ("EUR", "Евро"), ("CNY", "Юань"), ("GBP", "Фунт стерлингов")]
CRYPTO = [("BTC", "Биткоин", "bitcoin", "BTCUSDT"), ("ETH", "Эфириум", "ethereum", "ETHUSDT"),
          ("TON", "Toncoin", "the-open-network", "TONUSDT"), ("SOL", "Solana", "solana", "SOLUSDT")]
INDICES = [("IMOEX", "Индекс Мосбиржи"), ("SPX", "S&P 500"), ("NASDAQ", "NASDAQ Composite")]
ALL_CODES = [c for c, _ in FX] + [c[0] for c in CRYPTO]            # не больше 8 позиций
NAMES = {**dict(FX), **{c[0]: c[1] for c in CRYPTO}}

TTL = {"fx": 3 * 3600, "crypto": 3600, "indices": 3600, "macro": 24 * 3600}
RETRY = 600            # после неудачи пробуем снова не раньше, чем через 10 минут
_tasks: set = set()
_locks: dict = {}


# ---------- разбор ответов ----------

def _f(v) -> float | None:
    try:
        return float(str(v).replace(" ", "").replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def parse_cbr(raw: bytes) -> tuple[date, dict[str, float]]:
    """XML_daily.asp → (дата курса, {код: рублей за 1 единицу})."""
    root = ET.fromstring(raw)
    d = datetime.strptime(root.get("Date"), "%d.%m.%Y").date()
    out = {}
    for v in root.findall("Valute"):
        code = v.findtext("CharCode")
        unit = _f(v.findtext("VunitRate"))
        if unit is None:
            val, nom = _f(v.findtext("Value")), _f(v.findtext("Nominal")) or 1
            unit = val / nom if val is not None else None
        if code and unit:
            out[code] = unit
    if not out:
        raise ValueError("ЦБ: пустой список курсов")
    return d, out


def parse_coingecko(j: dict) -> dict[str, tuple[float, float | None]]:
    out = {}
    for code, _n, cg, _b in CRYPTO:
        row = (j or {}).get(cg) or {}
        if _f(row.get("usd")):
            out[code] = (_f(row["usd"]), _f(row.get("usd_24h_change")))
    return out


def parse_binance(rows: list) -> dict[str, tuple[float, float | None]]:
    by = {r.get("symbol"): r for r in rows or []}
    out = {}
    for code, _n, _cg, sym in CRYPTO:
        r = by.get(sym)
        if r and _f(r.get("lastPrice")):
            out[code] = (_f(r["lastPrice"]), _f(r.get("priceChangePercent")))
    return out


def parse_moex_history(j: dict) -> list[tuple[date, float]]:
    h = (j or {}).get("history") or {}
    cols = h.get("columns") or []
    i_d, i_c = cols.index("TRADEDATE"), cols.index("CLOSE")
    out = []
    for r in h.get("data") or []:
        v = _f(r[i_c])
        if v:
            out.append((date.fromisoformat(r[i_d]), v))
    return sorted(out)


def parse_stooq(text: str) -> list[tuple[date, float]]:
    rows = list(csv.reader(io.StringIO((text or "").strip())))
    if not rows or [c.strip().lower() for c in rows[0][:5]] != ["date", "open", "high", "low", "close"]:
        raise ValueError("Stooq: не CSV с котировками")
    out = []
    for r in rows[1:]:
        v = _f(r[4]) if len(r) > 4 else None
        if v:
            out.append((date.fromisoformat(r[0]), v))
    return sorted(out)


def parse_yahoo(j: dict) -> list[tuple[date, float]]:
    res = ((j or {}).get("chart") or {}).get("result") or []
    if not res:
        raise ValueError("Yahoo: нет данных")
    ts = res[0].get("timestamp") or []
    closes = (((res[0].get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    out = []
    for t, c in zip(ts, closes):
        if c:
            out.append((datetime.fromtimestamp(t, tz=timezone.utc).date(), float(c)))
    return sorted(out)


def _rows(html: str) -> list[list[str]]:
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html or "", re.S | re.I):
        cells = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)]
        if cells:
            out.append(cells)
    return out


def parse_key_rate(html: str) -> tuple[date, float]:
    """Страница «Ключевая ставка»: строки «ДД.ММ.ГГГГ | ставка», самая свежая — по дате."""
    best = None
    for r in _rows(html):
        if len(r) >= 2 and re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", r[0]) and _f(r[1]) is not None:
            d = datetime.strptime(r[0], "%d.%m.%Y").date()
            if best is None or d > best[0]:
                best = (d, _f(r[1]))
    if not best:
        raise ValueError("ЦБ: не нашёл ключевую ставку")
    return best


def parse_inflation(html: str) -> dict:
    """Страница «Инфляция и ключевая ставка»: «ММ.ГГГГ | ставка | инфляция г/г | цель»."""
    best = None
    for r in _rows(html):
        if len(r) >= 3 and re.fullmatch(r"\d{2}\.\d{4}", r[0]) and _f(r[2]) is not None:
            m, y = r[0].split(".")
            key = (int(y), int(m))
            if best is None or key > best[0]:
                best = (key, r)
    if not best:
        raise ValueError("ЦБ: не нашёл инфляцию")
    (y, m), r = best
    return {"inflation": _f(r[2]), "inflation_month": f"{y:04d}-{m:02d}", "target": _f(r[3]) if len(r) > 3 else None,
            "rate_month": _f(r[1])}


def stats(series: list[tuple[date, float]]) -> dict:
    """Значение, изменение за день и за месяц и точки для мини-графика."""
    if len(series) < 2:
        raise ValueError("мало точек")
    last_d, last = series[-1]
    prev = series[-2][1]
    month_ago = last_d - timedelta(days=30)
    base = next((v for d, v in series if d >= month_ago), series[0][1])
    return {"value": round(last, 2), "day_pct": round((last / prev - 1) * 100, 2),
            "month_pct": round((last / base - 1) * 100, 2), "date": last_d.isoformat(),
            "series": [round(v, 2) for _d, v in series[-30:]]}


# ---------- загрузка ----------

async def fetch_fx() -> dict:
    d, cur = parse_cbr(await net.get("https://www.cbr.ru/scripts/XML_daily.asp", as_="bytes"))
    prev = {}
    try:
        _d2, prev = parse_cbr(await net.get("https://www.cbr.ru/scripts/XML_daily.asp",
                                            {"date_req": (d - timedelta(days=1)).strftime("%d/%m/%Y")}, as_="bytes"))
    except Exception as e:
        log.warning("ЦБ: курсы за предыдущий день: %s", e)
    items = []
    for code, name in FX:
        if code not in cur:
            continue
        p = prev.get(code)
        items.append({"code": code, "name": name, "value": round(cur[code], 4),
                      "change_pct": round((cur[code] / p - 1) * 100, 2) if p else None})
    if not items:
        raise ValueError("ЦБ: нет нужных валют")
    return {"date": d.isoformat(), "items": items}


async def fetch_crypto() -> dict:
    src, got = "coingecko", {}
    try:
        got = parse_coingecko(await net.get("https://api.coingecko.com/api/v3/simple/price", {
            "ids": ",".join(c[2] for c in CRYPTO), "vs_currencies": "usd", "include_24hr_change": "true"}))
    except Exception as e:
        log.warning("CoinGecko: %s", e)
    if len(got) < len(CRYPTO):
        try:
            more = parse_binance(await net.get("https://api.binance.com/api/v3/ticker/24hr", {
                "symbols": json.dumps([c[3] for c in CRYPTO], separators=(",", ":"))}))
            if more and not got:
                src = "binance"
            got = {**more, **got}
        except Exception as e:
            log.warning("Binance: %s", e)
    if not got:
        raise ValueError("крипта: ни один источник не ответил")
    return {"src": src, "items": [{"code": c[0], "name": c[1], "usd": got[c[0]][0], "change_pct": got[c[0]][1]}
                                  for c in CRYPTO if c[0] in got]}


async def _moex() -> list[tuple[date, float]]:
    t = today()
    j = await net.get("https://iss.moex.com/iss/history/engines/stock/markets/index/securities/IMOEX.json", {
        "from": (t - timedelta(days=45)).isoformat(), "till": t.isoformat(), "iss.meta": "off",
        "history.columns": "TRADEDATE,CLOSE"})
    series = parse_moex_history(j)
    try:      # текущее значение внутри торгового дня
        m = await net.get("https://iss.moex.com/iss/engines/stock/markets/index/securities/IMOEX.json", {
            "iss.meta": "off", "iss.only": "marketdata", "marketdata.columns": "SECID,CURRENTVALUE"})
        cols, rows = m["marketdata"]["columns"], m["marketdata"]["data"]
        cur = _f(rows[0][cols.index("CURRENTVALUE")]) if rows else None
        if cur and series and series[-1][0] < t:
            series.append((t, cur))
        elif cur and series:
            series[-1] = (series[-1][0], cur)
    except Exception as e:
        log.debug("MOEX marketdata: %s", e)
    return series


async def _us(stooq_sym: str, yahoo_sym: str) -> list[tuple[date, float]]:
    t = today()
    try:
        return parse_stooq(await net.get("https://stooq.com/q/d/l/", {
            "s": stooq_sym, "i": "d", "d1": (t - timedelta(days=50)).strftime("%Y%m%d"), "d2": t.strftime("%Y%m%d")},
            as_="text"))
    except Exception as e:
        log.warning("Stooq %s: %s — пробую Yahoo", stooq_sym, e)
    return parse_yahoo(await net.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}",
                                     {"range": "2mo", "interval": "1d"}))


async def fetch_indices() -> dict:
    loaders = {"IMOEX": _moex, "SPX": lambda: _us("^spx", "^GSPC"), "NASDAQ": lambda: _us("^ndq", "^IXIC")}
    res = await asyncio.gather(*[loaders[i]() for i, _ in INDICES], return_exceptions=True)
    items = []
    for (code, name), r in zip(INDICES, res):
        if isinstance(r, Exception):
            log.warning("Индекс %s: %s", code, r)
            continue
        try:
            items.append({"id": code, "name": name, **stats(r)})
        except Exception as e:
            log.warning("Индекс %s: %s", code, e)
    if not items:
        raise ValueError("индексы: ни один источник не ответил")
    return {"items": items}


async def fetch_macro() -> dict:
    t = today()
    out: dict = {}
    infl = None
    try:
        infl = parse_inflation(await net.get("https://www.cbr.ru/hd_base/infl/", as_="text"))
        out.update(inflation=infl["inflation"], inflation_month=infl["inflation_month"], target=infl["target"])
    except Exception as e:
        log.warning("ЦБ, инфляция: %s", e)
    try:
        d, rate = parse_key_rate(await net.get("https://www.cbr.ru/hd_base/KeyRate/", {
            "UniDbQuery.Posted": "True", "UniDbQuery.From": (t - timedelta(days=30)).strftime("%d.%m.%Y"),
            "UniDbQuery.To": t.strftime("%d.%m.%Y")}, as_="text"))
        out.update(key_rate=rate, key_rate_date=d.isoformat())
    except Exception as e:
        log.warning("ЦБ, ключевая ставка: %s", e)
        if infl and infl.get("rate_month") is not None:
            out.update(key_rate=infl["rate_month"], key_rate_date=None)
    if not out:
        raise ValueError("ЦБ: ни ставка, ни инфляция не загрузились")
    return out


FETCH = {"fx": fetch_fx, "crypto": fetch_crypto, "indices": fetch_indices, "macro": fetch_macro}


# ---------- кэш в app_state ----------

async def _load(key: str) -> dict:
    v = await db.get_pool().fetchval("SELECT value FROM app_state WHERE key=$1", "market:" + key)
    if isinstance(v, str):
        v = json.loads(v or "{}")
    return v or {}


async def _save(key: str, rec: dict) -> None:
    await db.get_pool().execute(
        "INSERT INTO app_state (key, value, updated_at) VALUES ($1, $2::jsonb, now())"
        " ON CONFLICT (key) DO UPDATE SET value=$2::jsonb, updated_at=now()", "market:" + key, rec)


async def refresh(key: str) -> bool:
    """Загружает раздел и сохраняет. При ошибке прежние данные остаются, запоминается время попытки."""
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        rec = await _load(key)
        try:
            data = await asyncio.wait_for(FETCH[key](), 45)
        except Exception as e:
            log.warning("Рынок, %s: %s", key, e)
            rec["tried"] = time.time()
            await _save(key, rec)
            return False
        await _save(key, {"at": time.time(), "tried": time.time(), "data": data})
        return True


def _spawn(key: str) -> None:
    t = asyncio.create_task(refresh(key))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _due(rec: dict, key: str) -> bool:
    t = time.time()
    return t - rec.get("at", 0) >= TTL[key] and t - rec.get("tried", 0) >= RETRY


async def section(key: str) -> tuple[dict | None, bool]:
    """(данные, устарели ли). Есть кэш — отдаём сразу и обновляем в фоне; кэша нет — ждём первую загрузку."""
    rec = await _load(key)
    if rec.get("data") is None:
        if time.time() - rec.get("tried", 0) >= RETRY:
            await refresh(key)
            rec = await _load(key)
        return rec.get("data"), rec.get("data") is None
    if _due(rec, key):
        _spawn(key)
    return rec["data"], time.time() - rec.get("at", 0) > 2 * TTL[key]


async def refresh_due() -> None:
    """Фоновая задача: обновляет просроченные разделы, не задерживая остальные задачи."""
    for key in FETCH:
        if _due(await _load(key), key):
            _spawn(key)


# ---------- для панели ----------

def clean_hidden(v) -> list[str]:
    return [c for c in (v or []) if c in ALL_CODES]


async def overview(hidden: list[str] | None = None) -> dict:
    hidden = clean_hidden(hidden)
    (fx, fx_old), (cr, cr_old), (ix, ix_old), (mc, mc_old) = await asyncio.gather(
        section("fx"), section("crypto"), section("indices"), section("macro"))
    usd = next((i["value"] for i in (fx or {}).get("items", []) if i["code"] == "USD"), None)
    rates = [i for i in (fx or {}).get("items", []) if i["code"] not in hidden]
    coins = []
    for i in (cr or {}).get("items", []):
        if i["code"] in hidden:
            continue
        coins.append({**i, "rub": round(i["usd"] * usd, 2) if usd else None})
    return {"fx": {"date": (fx or {}).get("date"), "items": rates, "stale": fx_old},
            "crypto": {"items": coins, "stale": cr_old, "src": (cr or {}).get("src")},
            "indices": {"items": (ix or {}).get("items", []), "stale": ix_old},
            "macro": {**(mc or {}), "stale": mc_old},
            "catalog": [{"code": c, "name": NAMES[c], "on": c not in hidden} for c in ALL_CODES],
            "empty": not (fx or cr or ix or mc)}
