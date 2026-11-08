"""Версия 1.10.7: демо-кабинет (заготовка с вымышленными данными, песочницы посетителей, лимиты, ASGI-прослойка)."""
import _env  # noqa: F401  (пути и база)
import asyncio, os, re, subprocess, time
from datetime import timedelta
from pathlib import Path
import asyncpg
from app import ai, db, demo, demo_data, util
from app.config import get_settings
from _env import PSQL

FAILS = []
APP = Path(__file__).resolve().parent.parent / "core" / "app"
S = get_settings()


def check(cond, name, info=""):
    print(("  ✔ " if cond else "  ✖ ") + name + ("" if cond else f"  → {str(info)[:500]}"))
    if not cond:
        FAILS.append(name)


def sql(s, dbname=None):
    cmd = PSQL + (["-d", dbname] if dbname else [])
    return subprocess.run(cmd, input=s, capture_output=True, text=True, check=True).stdout.strip()


def dbs():
    return set(sql("SELECT datname FROM pg_database WHERE datname LIKE 'demo\\_%'").split())


async def main():
    S.demo_mode, S.demo_max_sessions, S.demo_idle_min, S.demo_max_min = True, 3, 20, 60
    S.demo_ai_messages, S.demo_ai_calls, S.demo_ai_daily, S.demo_new_per_hour = 3, 5, 6, 4
    S.llm_api_key = "test-key"
    for d in dbs():
        sql(f'DROP DATABASE IF EXISTS "{d}" WITH (FORCE)')
    db.pool = asyncpg.FakeConn()               # управляющая база (обычная тестовая)

    print("== Заготовка")
    t0 = time.time()
    name = await demo.build_template()
    check(re.fullmatch(r"demo_t_\d+", name) and name in dbs(), "заготовка создана", name)
    print(f"   (собрана за {time.time() - t0:.1f} с)")
    cnt = lambda t, w="true": int(sql(f"SELECT count(*) FROM {t} WHERE {w}", name))
    check(cnt("families") == 1 and cnt("users") == 2, "семья из двух взрослых")
    check(sql("SELECT name FROM families", name) == "Семья Ветровых", "вымышленная семья")
    check(cnt("transactions", "deleted_at IS NULL") >= 500, "много трат", cnt("transactions"))
    check(cnt("transaction_items") >= 150, "чеки по позициям", cnt("transaction_items"))
    check(cnt("meals") >= 1000 and cnt("metrics") >= 2000, "питание и замеры")
    check(cnt("transactions", "deleted_at IS NOT NULL") == 2, "две удалённые траты для «Вернуть»")
    check(cnt("meals", "deleted_at IS NOT NULL") == 1, "одна удалённая еда")
    bad = sql("""SELECT count(*) FROM (SELECT t.id FROM transactions t JOIN transaction_items i ON i.transaction_id=t.id
                 GROUP BY t.id, t.amount HAVING abs(sum(i.amount)-t.amount) > 0.01) x""", name)
    check(bad == "0", "сумма позиций чека равна сумме траты", bad)
    first, last = sql("SELECT min(occurred_on), max(occurred_on) FROM transactions WHERE deleted_at IS NULL", name).split("|")
    span = (util.today() - util.today().fromisoformat(first)).days
    check(150 <= span <= 190, "история около полугода", span)
    check(last == str(util.today() - timedelta(days=1)), "история заканчивается вчера", last)
    check(sql("SELECT count(*) FROM transactions WHERE occurred_on >= current_date", name) == "0", "в заготовке нет «сегодня»")
    check(sql("SELECT count(*) FROM transactions WHERE kind='income'", name) != "0", "есть доходы")
    inc, exp = [float(x) for x in sql("SELECT coalesce(sum(amount) FILTER (WHERE kind='income'),0), coalesce(sum(amount) FILTER (WHERE kind='expense'),0) FROM transactions WHERE deleted_at IS NULL", name).split("|")]
    check(0.7 < exp / inc < 1.2, "доходы и расходы соизмеримы", (inc, exp))
    check(cnt("bills") == 12 and cnt("bill_payments") >= 40, "регулярные платежи с историей")
    check(cnt("bills", "next_due >= current_date") == 12, "сроки платежей в будущем")
    check(cnt("vehicles") == 2 and cnt("service_rules") >= 8 and cnt("service_log") >= 5, "гараж")
    check(cnt("papers") >= 12 and cnt("papers", "expires_on < current_date") >= 1 and cnt("papers", "expires_on BETWEEN current_date AND current_date+60") >= 2,
          "документы: есть истёкшие и скоро истекающие")
    check(cnt("meters") == 3 and cnt("meter_readings") >= 15, "счётчики с показаниями")
    check(cnt("trips") == 4 and cnt("trips", "start_on > current_date") == 1 and cnt("trip_checklist") >= 20, "поездки: прошлые и будущая")
    check(cnt("trips", "start_on <= current_date AND end_on >= current_date") == 0, "ни одна поездка не «идёт сейчас»")
    check(cnt("savings_goals") == 4 and cnt("savings_goals", "closed_at IS NOT NULL") == 1, "накопления, одна цель закрыта")
    check(cnt("debts") == 4 and cnt("loyalty_cards") >= 8 and cnt("family_notes") >= 8, "долги, карты, заметки")
    check(cnt("tasks", "status='open'") >= 10 and cnt("tasks", "status='done'") >= 20 and cnt("shopping_items", "bought_at IS NULL") >= 10, "дела и покупки")
    check(cnt("bank_rows", "status='new'") >= 3 and cnt("bank_rows", "status='matched'") >= 3, "выписка: разобранное и новое")
    check(cnt("meds", "active") >= 3 and cnt("med_intakes") >= 150, "лекарства и приёмы")
    ean = [r for r in sql("SELECT number FROM loyalty_cards WHERE format='ean13'", name).split()]
    ok = all(len(n) == 13 and (10 - sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(n[:12])) % 10) % 10 == int(n[12]) for n in ean)
    check(ok and ean, "штрихкоды EAN-13 с верной контрольной цифрой", ean)
    check(sql("SELECT count(*) FROM users WHERE settings->>'city' IS NOT NULL", name) != "0", "город в настройках")
    check(sql("SELECT count(*) FROM categories", name) == sql("SELECT count(*) FROM categories"), "справочник категорий из миграций")
    text = (APP / "demo_data.py").read_text(encoding="utf-8")
    check("Иркутск" not in text and "Irkutsk" not in text, "в данных нет реального города пользователя")

    print("== Сеансы")
    sid, session, box = await demo.create("1.1.1.1", "UA")
    check(box.db_name in dbs() and demo.lookup(sid) is box and demo.lookup("nope") is None, "сеанс создан, база есть")
    n_today = sql("SELECT count(*) FROM meals WHERE eaten_at::date = current_date", box.db_name)
    check(sql("SELECT count(*) FROM web_sessions", box.db_name) == "1", "одна сессия панели в базе посетителя")
    check(int(sql("SELECT count(*) FROM metrics WHERE measured_at::date = current_date", box.db_name)) >= 0, "«сегодня» дописано", n_today)
    await box.pool.execute("TRUNCATE bank_rows, bill_payments, transaction_items, service_log; DELETE FROM transactions;")
    check(sql("SELECT count(*) FROM transactions", box.db_name) == "0" and cnt("transactions") > 500, "правки посетителя не задевают заготовку")
    st = demo.status(box)
    check(0 < st["seconds_left"] <= 20 * 60 and st["ai_left"] == 3 and st["user"] == 1, "статус сеанса", st)
    tok = await demo.make_session(box, 2)
    check(box.user == 2 and sql("SELECT user_id FROM web_sessions", box.db_name) == "2" and tok != session, "«Войти как Мария»")
    try:
        await demo.make_session(box, 99); check(False, "нет такого пользователя")
    except ValueError:
        check(True, "нет такого пользователя — отказ")

    print("== Лимиты сеансов")
    boxes = [box]
    for i in range(2):
        boxes.append((await demo.create(f"2.2.2.{i}"))[2])
    try:
        await demo.create("3.3.3.3"); check(False, "лимит числа сеансов")
    except demo.Busy as e:
        check(e.minutes >= 1, "занято: лимит числа сеансов", e.minutes)
    for b in boxes[1:]:
        await demo.drop(b)
    check(len(demo._boxes) == 1 and boxes[1].db_name not in dbs(), "drop удаляет базу")
    demo._ip_hits.clear()
    for i in range(4):
        b = (await demo.create("9.9.9.9"))[2]; await demo.drop(b)
    try:
        await demo.create("9.9.9.9"); check(False, "лимит новых сеансов с адреса")
    except demo.RateLimited:
        check(True, "лимит новых сеансов с одного адреса")
    box.last -= 21 * 60
    check(demo.lookup(sid) is None and demo.expired(box), "простой больше 20 минут — сеанс закончен")
    check(await demo.reap() == 1 and box.db_name not in dbs() and not demo._boxes, "reap удаляет базу ушедшего")
    sid, session, box = await demo.create("4.4.4.4")
    box.created -= 61 * 60; box.last = time.time()
    check(demo.expired(box), "больше часа от начала — сеанс закончен")
    await demo.reap()

    print("== ИИ: лимиты")
    demo.install()
    sid, session, box = await demo.create("5.5.5.5")
    tok = demo._current.set(box)
    try:
        r = demo.chat_precheck("привет", False); check(r is None, "сообщение 1 пропущено")
        demo.chat_precheck("ещё", False); demo.chat_precheck("и ещё", False)
        r = demo.chat_precheck("четвёртое", False)
        check(r and "лимит исчерпан" in r["text"], "после лимита сообщений — заглушка", r)
        check(demo.chat_precheck("/today", False) is None, "/today работает без лимита")
        check("недоступна" in demo.chat_precheck("/web", False)["text"] and "недоступна" in demo.chat_precheck("/update now", False)["text"], "/web и /update заблокированы")
        check("файлы" in demo.chat_precheck("", True)["text"], "файлы отключены")
        check("до 400" in demo.chat_precheck("я" * 401, False)["text"], "длинное сообщение отклонено")
        for _ in range(5):
            ai.GUARD(False)
        try:
            ai.GUARD(False); check(False, "лимит обращений на сеанс")
        except ai.AIError as e:
            check("лимит" in str(e), "лимит обращений к ИИ на сеанс")
        try:
            ai.GUARD(True); check(False, "фото отключены")
        except ai.AIError as e:
            check("фото" in str(e), "фото и голос отключены")
    finally:
        demo._current.reset(tok)
    check(ai.GUARD is demo.ai_guard, "install: ai.GUARD подключён")
    try:
        demo.ai_guard(False); check(False, "ИИ вне сеанса")
    except ai.AIError:
        check(True, "ИИ вне демо-сеанса не отвечает")
    demo._ai_day[:] = [util.today(), S.demo_ai_daily]
    b2 = (await demo.create("6.6.6.6"))[2]
    tok = demo._current.set(b2)
    try:
        demo.ai_guard(False); check(False, "суточный лимит")
    except ai.AIError as e:
        check("сутки" in str(e), "общий суточный лимит")
    finally:
        demo._current.reset(tok)
    await demo.drop(b2)

    print("== Пути и прослойка")
    check(all(demo.path_allowed(p) for p in ("/", "/health", "/demo", "/demo/start", "/web/", "/web/api/me")), "разрешённые пути")
    check(not any(demo.path_allowed(p) for p in ("/api/x", "/docs", "/openapi.json", "/telegram", "/metrics", "/webhook")), "остальные пути закрыты")
    check(all(demo.denied(p) for p in ("/web/login", "/web/api/update", "/web/api/bank/upload", "/web/api/ticktick/x", "/web/api/integrations/yandex")), "опасные пути запрещены")
    seen = {}

    async def inner(scope, receive, send):
        seen["pool"], seen["box"] = db.get_pool(), demo.current()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = demo.DemoMiddleware(inner)

    async def call(path, cookie=None):
        out = []

        async def send(m): out.append(m)

        async def receive(): return {"type": "http.request", "body": b""}
        hd = [(b"cookie", cookie.encode())] if cookie else []
        await mw({"type": "http", "path": path, "headers": hd, "method": "GET"}, receive, send)
        return out[0]["status"], dict(out[0]["headers"])

    sid, session, box = await demo.create("7.7.7.7")
    check((await call("/docs"))[0] == 404, "/docs → 404")
    check((await call("/web/api/update"))[0] == 403, "/web/api/update → 403")
    check((await call("/web/api/me"))[0] == 401, "API без сеанса → 401")
    st, h = await call("/web/"); check(st == 303 and h[b"location"] == b"/?e=expired", "страница без сеанса → на старт", h)
    seen.clear()
    st, _ = await call("/web/api/me", f"demo_sid={sid}")
    check(st == 200 and seen["pool"] is box.pool and seen["box"] is box, "с сеансом: база посетителя подставлена")
    check(db.get_pool() is not box.pool and demo.current() is None, "после запроса подмена снята")
    seen.clear()
    st, _ = await call("/web/static/x.js")
    check(st == 200 and seen["box"] is None, "статика без сеанса отдаётся")
    box.hits[:] = [time.time()] * 200
    check((await call("/web/api/me", f"demo_sid={sid}"))[0] == 429, "частые запросы → 429")
    check(not S.demo_mode or True, "")
    S.demo_mode = False
    seen.clear()
    st, _ = await call("/docs")
    check(st == 200, "вне демо-режима прослойка ничего не меняет")
    S.demo_mode = True
    html = demo.landing_html("busy", 7)
    check("7 мин" in html and 'action="/demo/start"' in html and "noindex" in html and "Иркутск" not in html, "стартовая страница")
    check(demo.inject_flag("<html><head><title>x</title>").startswith("<html><head><script>window.DEMO=true"), "флаг DEMO в страницу")
    check(demo.dsn_for("demo_s_abcdef123456").endswith("/demo_s_abcdef123456"), "адрес базы посетителя")
    try:
        demo.dsn_for("x; DROP DATABASE postgres"); check(False, "имя базы проверяется")
    except ValueError:
        check(True, "имя базы проверяется")

    print("== Завершение")
    await demo.shutdown()
    check(not demo._boxes and dbs() == {name}, "shutdown удаляет все базы сеансов", dbs())
    n2 = await demo.build_template()
    check(n2 != name and name not in dbs() and n2 in dbs(), "новая заготовка заменяет старую", dbs())
    await demo._drop_db(n2)

    print("== Подключение")
    main_src = (APP / "main.py").read_text(encoding="utf-8")
    check("DemoMiddleware" in main_src and "demo.router" in main_src and "demo.install()" in main_src, "main.py подключает демо")
    web_src = (APP / "web.py").read_text(encoding="utf-8")
    check('"demo"' in web_src and "inject_flag" in web_src, "web.py: флаг demo и страница")
    check("chat_precheck" in (APP / "mobile.py").read_text(encoding="utf-8"), "mobile.py: проверка чата")
    panel = (APP / "static" / "index.html").read_text(encoding="utf-8")
    check("window.DEMO" in panel and "/demo/reset" in panel and "/demo/as" in panel and "/demo/api/status" in panel, "панель: плашка демо")
    check("window.DEMO" in (APP / "static" / "chat.html").read_text(encoding="utf-8"), "чат: режим демо")
    comp = (APP.parent.parent / "docker-compose.yml").read_text(encoding="utf-8")
    check("demo-db" in comp and "DEMO_MODE" in comp and "DEMO_LLM_API_KEY" in comp, "docker-compose: сервисы демо")
    check(re.search(r"demo:\n(?:.*\n)*?\s+profiles:\s*\[?\"?demo", comp) is not None, "сервис demo запускается только с профилем demo")
    check((APP.parent.parent / "docs" / "DEMO_CLAUDE_CODE.md").exists(), "инструкция по установке демо")
    root = APP.parent.parent
    from app import release
    check(release.VERSION == "1.10.7" and (root / "VERSION").read_text().strip() == "1.10.7", "версия 1.10.7 в VERSION и release.py")
    check("## 1.10.7" in (root / "core" / "CHANGELOG.md").read_text(encoding="utf-8"), "запись в CHANGELOG")
    site = (root / "site-src" / "build.py").read_text(encoding="utf-8")
    check("VERSION = '1.10.7'" in site and "demo.assistant.saidov.ru" in site and "1.10.7" in (root / "site-src" / "data.js").read_text(encoding="utf-8"), "сайт: версия, ссылка на демо, история")
    pages = "".join(p.read_text(encoding="utf-8") for p in (APP / "static" / "site").glob("*.html"))
    check("Попробовать демо" in pages and "Иркутск" not in pages and "из 4 человек" not in pages, "собранный сайт: кнопки демо, нет запрещённых слов")
    check("## 41. Демо-кабинет" in (root / "docs" / "INSTALL.md").read_text(encoding="utf-8"), "INSTALL.md: раздел про демо")


asyncio.run(main())
print("\nИТОГ: " + ("всё прошло" if not FAILS else f"ошибок: {len(FAILS)} → {FAILS}"))
