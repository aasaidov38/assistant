"""Версия 1.10: рынок (курсы, крипта, индексы, ставка и инфляция), бури Kp, пыльца, базовый город — на PostgreSQL.
Внешние сервисы подменены ответами в их настоящем формате."""
import _env  # noqa: F401  (пути и база)
import asyncio, json, subprocess, time
from datetime import date, datetime, timedelta, timezone
import asyncpg
from app import db, envdata, jobs, market, morning, net, prefs, users, web
from app.util import today

from _env import PSQL
sql = lambda s: subprocess.run(PSQL, input=s, capture_output=True, text=True, check=True).stdout.strip()
FAILS = []


def check(cond, name, info=""):
    print(("  ✔ " if cond else "  ✖ ") + name + ("" if cond else f"  → {str(info)[:500]}"))
    if not cond:
        FAILS.append(name)


SEED = """
TRUNCATE families, users, user_identities, app_state RESTART IDENTITY CASCADE;
INSERT INTO families (name) VALUES ('Демо');
INSERT INTO users (family_id, name) VALUES (1,'Алексей'), (1,'Мария');
"""

# ---------- ответы источников в их настоящем формате ----------
def cbr_xml(day: str, usd: str, eur="90,4635") -> bytes:
    x = (f'<?xml version="1.0" encoding="windows-1251"?><ValCurs Date="{day}" name="Foreign Currency Market">'
         f'<Valute ID="R01235"><NumCode>840</NumCode><CharCode>USD</CharCode><Nominal>1</Nominal><Name>Доллар США</Name>'
         f'<Value>{usd}</Value><VunitRate>{usd}</VunitRate></Valute>'
         f'<Valute ID="R01239"><NumCode>978</NumCode><CharCode>EUR</CharCode><Nominal>1</Nominal><Name>Евро</Name>'
         f'<Value>{eur}</Value><VunitRate>{eur}</VunitRate></Valute>'
         f'<Valute ID="R01375"><NumCode>156</NumCode><CharCode>CNY</CharCode><Nominal>10</Nominal><Name>Юань</Name>'
         f'<Value>121,7140</Value></Valute>'
         f'<Valute ID="R01035"><NumCode>826</NumCode><CharCode>GBP</CharCode><Nominal>1</Nominal><Name>Фунт</Name>'
         f'<Value>108,4801</Value><VunitRate>108,4801</VunitRate></Valute></ValCurs>')
    return x.encode("cp1251")


def series(n=40, start=100.0, step=1.0):
    t = today()
    return [(t - timedelta(days=n - 1 - i), start + i * step) for i in range(n)]


def moex_json(s):
    return {"history": {"columns": ["TRADEDATE", "CLOSE"], "data": [[d.isoformat(), v] for d, v in s]}}


def stooq_csv(s):
    return "Date,Open,High,Low,Close,Volume\n" + "\n".join(f"{d.isoformat()},1,1,1,{v},0" for d, v in s)


def yahoo_json(s):
    return {"chart": {"result": [{"timestamp": [int(datetime(d.year, d.month, d.day, 14, tzinfo=timezone.utc).timestamp()) for d, _ in s],
                                  "indicators": {"quote": [{"close": [v for _, v in s]}]}}]}}


KEY_HTML = """<table class="data"><tr><th>Дата</th><th>Ставка</th></tr>
<tr><td>29.10.2026</td><td>14,00</td></tr><tr><td>02.11.2026</td><td>13,50</td></tr><tr><td>30.10.2026</td><td>14,00</td></tr></table>"""
INFL_HTML = """<table class="data"><tr><th>Дата</th><th>Ключевая ставка, % годовых</th><th>Инфляция, % г/г</th><th>Цель по инфляции, %</th></tr>
<tr><td>08.2026</td><td>14,00</td><td>6,33</td><td>4,00</td></tr><tr><td>09.2026</td><td>14,00</td><td>6,35</td><td>4,00</td></tr></table>"""

ROUTES, CALLS = {}, []


async def fake_get(url, params=None, as_="json"):
    CALLS.append((url, dict(params or {})))
    h = ROUTES.get(url)
    if h is None:
        raise RuntimeError("нет сети: " + url)
    v = h(params or {}) if callable(h) else h
    if isinstance(v, Exception):
        raise v
    return v


class Resp:
    def __init__(self, j): self.j = j
    def raise_for_status(self): pass
    def json(self): return self.j


class FakeWeather:
    def __init__(self): self.calls = []
    async def get(self, url, params=None):
        self.calls.append(params)
        hours = [f"2026-11-04T{h:02d}:00" if h < 24 else f"2026-11-05T{h - 24:02d}:00" for h in range(48)]
        return Resp({"utc_offset_seconds": 10800, "current": {"time": "2026-11-04T09:30", "temperature_2m": 3, "apparent_temperature": 1,
                     "weather_code": 3, "wind_speed_10m": 4, "relative_humidity_2m": 80, "precipitation": 0},
                     "hourly": {"time": hours, "temperature_2m": list(range(48)), "weather_code": [3] * 48, "precipitation_probability": [0] * 48},
                     "daily": {"time": ["2026-11-04"] * 7, "weather_code": [3] * 7, "temperature_2m_min": [0] * 7, "temperature_2m_max": [5] * 7,
                               "precipitation_sum": [0] * 7, "wind_speed_10m_max": [5] * 7, "sunrise": ["2026-11-04T07:30"] * 7, "sunset": ["2026-11-04T17:10"] * 7}})


async def main():
    db.pool = asyncpg.FakeConn()
    sql(SEED)
    net.get = fake_get
    alex = await users.get(1)
    today_s = today().strftime("%d.%m.%Y")

    print("1. Курсы ЦБ")
    d, cur = market.parse_cbr(cbr_xml(today_s, "80,9734"))
    check(d == today() and cur["USD"] == 80.9734 and abs(cur["CNY"] - 12.1714) < 1e-9, "XML ЦБ (windows-1251, запятая, номинал 10 без VunitRate)", cur)
    prev_day = (today() - timedelta(days=1)).strftime("%d/%m/%Y")
    ROUTES["https://www.cbr.ru/scripts/XML_daily.asp"] = lambda p: cbr_xml((today() - timedelta(days=1)).strftime("%d.%m.%Y"), "80,0000") if p.get("date_req") else cbr_xml(today_s, "80,9734")
    fx = await market.fetch_fx()
    usd = next(i for i in fx["items"] if i["code"] == "USD")
    check([i["code"] for i in fx["items"]] == ["USD", "EUR", "CNY", "GBP"] and usd["change_pct"] == 1.22
          and any(c[1].get("date_req") == prev_day for c in CALLS), "курсы + изменение к предыдущему дню", fx)

    print("2. Крипта")
    CG = {"bitcoin": {"usd": 62800, "usd_24h_change": -3.0}, "ethereum": {"usd": 1862.6, "usd_24h_change": -2.8},
          "the-open-network": {"usd": 1.39, "usd_24h_change": -1.8}, "solana": {"usd": 150.5, "usd_24h_change": 0.5}}
    ROUTES["https://api.coingecko.com/api/v3/simple/price"] = CG
    cr = await market.fetch_crypto()
    check(cr["src"] == "coingecko" and [i["code"] for i in cr["items"]] == ["BTC", "ETH", "TON", "SOL"] and cr["items"][0]["usd"] == 62800, "CoinGecko", cr)
    ROUTES["https://api.coingecko.com/api/v3/simple/price"] = RuntimeError("429")
    ROUTES["https://api.binance.com/api/v3/ticker/24hr"] = [{"symbol": "BTCUSDT", "lastPrice": "62810.10", "priceChangePercent": "-2.9"},
                                                            {"symbol": "ETHUSDT", "lastPrice": "1860.00", "priceChangePercent": "-2.7"}]
    cr = await market.fetch_crypto()
    check(cr["src"] == "binance" and len(cr["items"]) == 2 and cr["items"][0]["usd"] == 62810.10, "CoinGecko молчит — запасной Binance", cr)
    ROUTES["https://api.binance.com/api/v3/ticker/24hr"] = RuntimeError("down")
    try:
        await market.fetch_crypto(); bad = False
    except ValueError:
        bad = True
    check(bad, "оба источника молчат — ошибка (а не пустой ответ)")

    print("3. Индексы")
    s40 = series()
    st = market.stats(s40)
    check(st["value"] == 139.0 and st["day_pct"] == round(139 / 138 * 100 - 100, 2) and len(st["series"]) == 30
          and st["month_pct"] == round((139 / 109 - 1) * 100, 2), "значение, день, месяц, 30 точек для графика", st)
    check(market.parse_moex_history(moex_json(s40))[-1][1] == 139.0, "Мосбиржа: история")
    check(market.parse_stooq(stooq_csv(s40))[-1][1] == 139.0 and market.parse_yahoo(yahoo_json(s40))[-1][1] == 139.0, "Stooq и Yahoo: разбор")
    try:
        market.parse_stooq("Get your apikey: ..."); bad = False
    except ValueError:
        bad = True
    check(bad, "Stooq вернул не CSV (например, просьбу про ключ) — ошибка")
    ROUTES["https://iss.moex.com/iss/history/engines/stock/markets/index/securities/IMOEX.json"] = moex_json(series(start=2800, step=2))
    ROUTES["https://iss.moex.com/iss/engines/stock/markets/index/securities/IMOEX.json"] = {
        "marketdata": {"columns": ["SECID", "CURRENTVALUE"], "data": [["IMOEX", 2900.5]]}}
    ROUTES["https://stooq.com/q/d/l/"] = lambda p: RuntimeError("blocked") if p["s"] == "^spx" else "Get your apikey"
    ROUTES["https://query1.finance.yahoo.com/v8/finance/chart/^GSPC"] = yahoo_json(series(start=6000, step=5))
    ix = await market.fetch_indices()
    ids = [i["id"] for i in ix["items"]]
    imoex = ix["items"][0]
    check(ids == ["IMOEX", "SPX"] and imoex["value"] == 2900.5, "Мосбиржа с текущим значением; S&P через Yahoo; NASDAQ недоступен — остальные на месте", ix)
    ROUTES["https://iss.moex.com/iss/history/engines/stock/markets/index/securities/IMOEX.json"] = RuntimeError("x")
    ROUTES["https://query1.finance.yahoo.com/v8/finance/chart/^GSPC"] = RuntimeError("x")
    try:
        await market.fetch_indices(); bad = False
    except ValueError:
        bad = True
    check(bad, "все индексы недоступны — ошибка")

    print("4. Ключевая ставка и инфляция")
    check(market.parse_key_rate(KEY_HTML) == (date(2026, 11, 2), 13.5), "ставка: берётся самая свежая по дате, а не первая строка")
    inf = market.parse_inflation(INFL_HTML)
    check(inf["inflation"] == 6.35 and inf["inflation_month"] == "2026-09" and inf["target"] == 4.0, "инфляция: последний месяц", inf)
    ROUTES["https://www.cbr.ru/hd_base/infl/"] = INFL_HTML
    ROUTES["https://www.cbr.ru/hd_base/KeyRate/"] = KEY_HTML
    mc = await market.fetch_macro()
    check(mc["key_rate"] == 13.5 and mc["key_rate_date"] == "2026-11-02" and mc["inflation"] == 6.35, "ставка и инфляция вместе", mc)
    ROUTES["https://www.cbr.ru/hd_base/KeyRate/"] = RuntimeError("x")
    mc = await market.fetch_macro()
    check(mc["key_rate"] == 14.0 and mc["inflation"] == 6.35, "страница ставки не открылась — ставка из таблицы инфляции", mc)

    print("5. Кэш в базе и показ последних данных")
    ROUTES["https://www.cbr.ru/hd_base/KeyRate/"] = KEY_HTML
    ROUTES["https://api.coingecko.com/api/v3/simple/price"] = CG
    ROUTES["https://iss.moex.com/iss/history/engines/stock/markets/index/securities/IMOEX.json"] = moex_json(series(start=2800, step=2))
    ROUTES["https://query1.finance.yahoo.com/v8/finance/chart/^GSPC"] = yahoo_json(series(start=6000, step=5))
    CALLS.clear()
    data, stale = await market.section("macro")
    n1 = len(CALLS)
    data2, _ = await market.section("macro")
    check(data["inflation"] == 6.35 and not stale and n1 >= 2 and len(CALLS) == n1, "первый вызов грузит, второй берёт из базы", (n1, len(CALLS)))
    check(sql("select count(*) from app_state where key='market:macro'") == "1", "кэш лежит в app_state")
    sql("UPDATE app_state SET value = jsonb_set(jsonb_set(value, '{at}', '1'), '{tried}', '1') WHERE key='market:macro'")
    CALLS.clear()
    data3, stale3 = await market.section("macro")
    await asyncio.sleep(0.6)
    check(data3["inflation"] == 6.35 and len(CALLS) >= 2, "устарело — данные отдаются сразу, обновление идёт в фоне", len(CALLS))
    # источник упал: старые данные остаются
    sql("UPDATE app_state SET value = jsonb_set(jsonb_set(value, '{at}', '1'), '{tried}', '1') WHERE key='market:macro'")
    ROUTES["https://www.cbr.ru/hd_base/infl/"] = RuntimeError("503")
    ROUTES["https://www.cbr.ru/hd_base/KeyRate/"] = RuntimeError("503")
    await market.refresh("macro")
    rec = json.loads(sql("select value from app_state where key='market:macro'"))
    check(rec["data"]["inflation"] == 6.35 and rec["tried"] > rec["at"], "источник упал — прежние данные не потеряны, время попытки записано", rec)
    CALLS.clear()
    d4, st4 = await market.section("macro")
    await asyncio.sleep(0.2)
    check(d4["inflation"] == 6.35 and not CALLS, "после неудачи 10 минут не долбим источник", CALLS)
    ROUTES["https://www.cbr.ru/hd_base/infl/"] = INFL_HTML
    ROUTES["https://www.cbr.ru/hd_base/KeyRate/"] = KEY_HTML

    print("6. Что видит человек")
    ov = await market.overview(["EUR", "TON", "ХХХ"])
    codes = [i["code"] for i in ov["fx"]["items"]] + [i["code"] for i in ov["crypto"]["items"]]
    btc = next(i for i in ov["crypto"]["items"] if i["code"] == "BTC")
    check("EUR" not in codes and "TON" not in codes and "USD" in codes and "SOL" in codes, "скрытые позиции не показываются", codes)
    check(btc["rub"] == round(62800 * 80.9734, 2) and len(ov["catalog"]) == 8, "рубли по курсу ЦБ; в каталоге ровно 8 позиций", btc)
    check(not ov["empty"] and ov["macro"]["key_rate"] == 13.5 and len(ov["indices"]["items"]) == 2, "ставка, инфляция, индексы на месте")
    check(market.clean_hidden(["USD", "bad", None]) == ["USD"], "чужие коды отбрасываются")

    print("7. Магнитные бури")
    now = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
    rows = []
    t = datetime(2026, 11, 3, 0, 0)
    while t <= datetime(2026, 11, 7, 0, 0):
        kp = 6.33 if t == datetime(2026, 11, 5, 9, 0) else 1.67 if t == datetime(2026, 11, 4, 9, 0) else 2.0
        rows.append({"time_tag": t.isoformat(), "kp": kp, "observed": "observed" if t <= datetime(2026, 11, 4, 9, 0) else "predicted", "noaa_scale": None})
        t += timedelta(hours=3)
    k0 = envdata.parse_kp(rows, 0, now)
    check(k0["now"]["kp"] == 1.7 and k0["now"]["label"] == "спокойно", "текущий Kp — последний начавшийся блок", k0["now"])
    check([d["date"] for d in k0["days"]] == ["2026-11-04", "2026-11-05", "2026-11-06"] and k0["days"][1]["storm"] and k0["days"][1]["max"] == 6.3
          and k0["days"][1]["label"] == "умеренная буря (G2)" and k0["days"][1]["peak"] == "09:00" and k0["storm"], "буря G2 завтра, пик 09:00", k0["days"])
    k8 = envdata.parse_kp(rows, 8 * 3600, now)
    check(k8["days"][1]["peak"] == "17:00", "для города с UTC+8 пик по местному времени 17:00", k8["days"][1])
    check(envdata.kp_label(4.9) == "слегка неспокойно" and envdata.kp_label(5) == "малая буря (G1)" and envdata.kp_label(9) == "экстремальная буря (G5)", "шкала G1–G5")
    calm = [dict(r, kp=2.0) for r in rows]
    check(not envdata.parse_kp(calm, 0, now)["storm"], "спокойный день — без бури")

    print("8. Пыльца")
    now_p = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    hours = [(datetime(2026, 5, 10) + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00") for h in range(48)]
    hourly = {"time": hours, "birch_pollen": [0] * 12 + [10, 120] + [5] * 34, "grass_pollen": [3.4] * 48, "alder_pollen": [0] * 48,
              "olive_pollen": [None] * 48, "mugwort_pollen": [0] * 48, "ragweed_pollen": [None] * 48}
    pl = envdata.parse_pollen({"hourly": hourly}, 7200, now_p)
    check(pl["covered"] and pl["items"][0]["name"] == "берёза" and pl["items"][0]["label"] == "высокая" and pl["items"][0]["value"] == 120
          and pl["items"][1]["name"] == "злаковые травы" and pl["items"][1]["label"] == "слабая" and len(pl["items"]) == 2, "берёза высокая, злаки слабые, нулевые не показываются", pl)
    none = envdata.parse_pollen({"hourly": {"time": hours, **{k: [None] * 48 for k in hourly if k != "time"}}}, 0, now_p)
    check(not none["covered"], "все значения пустые — вне покрытия модели")
    zero = envdata.parse_pollen({"hourly": {"time": hours, **{k: [0] * 48 for k in hourly if k != "time"}}}, 0, now_p)
    check(zero["covered"] and not zero["items"], "покрытие есть, пыльцы нет")

    print("9. Поиск города")
    ROUTES["https://geocoding-api.open-meteo.com/v1/search"] = {"results": [
        {"name": "Москва", "latitude": 55.75222, "longitude": 37.61556, "country": "Россия", "admin1": "Москва", "timezone": "Europe/Moscow"},
        {"name": "Москва", "latitude": 46.73, "longitude": -117.0, "country": "США", "admin1": "Айдахо", "timezone": "America/Los_Angeles"}, {"bad": 1}]}
    res = await envdata.search_city("Москва")
    check(len(res) == 2 and res[0]["label"] == "Москва, Россия" and res[1]["label"] == "Москва, Айдахо, США" and res[0]["lat"] == 55.7522, "поиск: метки с регионом и страной", res)
    check(await envdata.search_city("М") == [], "слишком короткий запрос")

    print("10. Базовый город и настройки")
    loc = await prefs.location(1)
    check(not loc["custom"] and loc["name"] == "Иркутск", "по умолчанию — общий город из .env", loc)
    r = await prefs.update(alex, {"city": {"name": "Москва", "label": "Москва, Россия", "lat": 55.7522, "lon": 37.6156, "tz": "Europe/Moscow"},
                                  "market_hidden": ["EUR", "nope", "BTC"]})
    check(r["city"]["name"] == "Москва" and r["market_hidden"] == ["EUR", "BTC"] and r["default_place"] == "Иркутск" and len(r["market_catalog"]) == 8,
          "город и скрытые позиции сохранены, лишнее отброшено", r)
    loc = await prefs.location(1)
    loc2 = await prefs.location(2)
    check(loc["custom"] and loc["lat"] == 55.7522 and not loc2["custom"], "у Алексея свой город, у Марии общий")
    r = await prefs.update(alex, {"city": {"name": "Плохой", "lat": 999, "lon": 0}})
    check(r["city"] is None, "неверные координаты отвергнуты (город сброшен)", r["city"])
    await prefs.update(alex, {"city": {"name": "Москва", "lat": 55.7522, "lon": 37.6156}})
    r = await prefs.update(alex, {"city": None})
    check(r["city"] is None and not (await prefs.location(1))["custom"], "«Вернуть общий» работает")
    check("storm" in prefs.TOPICS and (await prefs.get(1))["notify"]["storm"], "тема уведомлений «бури» есть и включена")

    print("11. Погода по городу")
    fw = FakeWeather()
    morning._client = fw
    morning._cache.clear(); morning._offsets.clear()
    msk = {"name": "Москва", "lat": 55.7522, "lon": 37.6156, "tz": "Europe/Moscow", "custom": True}
    f = await morning.forecast(msk)
    check(f["place"] == "Москва" and fw.calls[-1]["timezone"] == "auto" and fw.calls[-1]["latitude"] == 55.7522 and f["hours"][0]["time"] == "09:00"
          and f["utc_offset"] == 10800, "чужой город: часовой пояс auto, часы по его местному времени", (fw.calls[-1], f["hours"][:1]))
    await morning.forecast(msk)
    await morning.forecast(None)
    check(len(fw.calls) == 2 and fw.calls[-1]["timezone"] == "Asia/Irkutsk", "кэш отдельно для каждого места; общий город — как раньше", len(fw.calls))

    print("12. Строка о буре в утренней сводке")
    envdata._cache.clear()
    storm_rows = [dict(r) for r in rows]
    # окно: «сегодня» по часам сервера; подставим бурю на текущий и следующий день
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    storm_rows = [{"time_tag": (base + timedelta(hours=3 * i - 3)).isoformat(), "kp": 6.0 if i in (1, 2) else 2.0,
                   "observed": "observed" if i == 0 else "predicted", "noaa_scale": None} for i in range(30)]
    envdata._cache["kp"] = (time.time(), storm_rows)
    line = await morning.storm_line(1, 0)
    check(line.startswith("🧲 Магнитная буря:") and "Kp 6" in line and "умеренная буря (G2)" in line, "буря есть — строка в сводке", line)
    await prefs.update(alex, {"notify": {"storm": False}})
    check(await morning.storm_line(1, 0) == "", "человек выключил тему — строки нет")
    await prefs.update(alex, {"notify": {"storm": True}})
    envdata._cache["kp"] = (time.time(), [dict(r, kp=2.0) for r in storm_rows])
    check(await morning.storm_line(1, 0) == "", "спокойно — строки нет")
    envdata._cache.pop("kp")

    print("13. Панель: API")
    async def fake_forecast(loc=None): return {"place": loc["name"], "utc_offset": 0, "now": {}, "hours": [], "days": [{"date": "2026-11-04"}]}
    morning.forecast = fake_forecast
    envdata._cache["kp"] = (time.time(), storm_rows)
    envdata._cache[("pollen", 55.8, 37.6)] = (time.time(), {"hourly": {"time": hours, **{k: [None] * 48 for k in hourly if k != "time"}}})
    await prefs.update(alex, {"city": {"name": "Москва", "lat": 55.7522, "lon": 37.6156}})
    w = await web.weather(user=alex)
    check(w["place"] == "Москва" and w["custom"] and w["storm"]["now"] and w["pollen"] is None, "погода: город человека, бури есть, пыльцы нет (нет данных)", w.keys())
    envdata._cache[("pollen", 55.8, 37.6)] = (time.time(), {"hourly": hourly})
    w = await web.weather(user=alex)
    check(w["pollen"] and w["pollen"]["covered"], "для города с данными пыльца показывается")
    await prefs.update(alex, {"market_hidden": ["USD"]})
    m = await web.market_data(user=alex)
    check("USD" not in [i["code"] for i in m["fx"]["items"]] and m["macro"]["key_rate"] == 13.5, "рынок: скрытое не отдаётся")
    cs = await web.city_search(q="Москва", user=alex)
    check(len(cs) == 2, "поиск города через API")

    print("14. Фоновая задача и сайт")
    check(market.refresh_due in jobs.loop.__code__.co_consts or "market.refresh_due" in open(jobs.__file__).read(), "обновление рынка подключено к фоновым задачам")
    await market.refresh_due()
    check(True, "refresh_due не падает")

    print("15. Регрессия 1.10.1: повторные функции в панели")
    import re, pathlib
    src = pathlib.Path(__file__).resolve().parent.parent.joinpath("core/app/static/index.html").read_text(encoding="utf-8")
    names = re.findall(r"^(?:async )?function (\w+)\(", src, re.M)
    dup = sorted({n for n in names if names.count(n) > 1})
    check(not dup, "в панели нет двух функций с одним именем (перекрытие ломало главную)", dup)

    print("ИТОГ:", "всё прошло" if not FAILS else f"ошибок {len(FAILS)}: {FAILS}")


asyncio.run(main())

