"""Версия 1.10.2: баланс GPTunnel (запрос, история и прогноз, уведомления владельцу), новый значок и заставка — на PostgreSQL."""
import os
os.environ.update(LLM_PROVIDER="openai", LLM_API_KEY="sk-test-key", LLM_BASE_URL="https://gptunnel.ru/v1")
import _env  # noqa: F401  (пути и база)
import asyncio, subprocess, time
import asyncpg
from pathlib import Path
from app import balance, db, jobs, net, notify, prefs, users, web
from app.config import get_settings

from _env import PSQL
sql = lambda s: subprocess.run(PSQL, input=s, capture_output=True, text=True, check=True).stdout.strip()
FAILS, SENT, CALLS = [], [], []
ROOT = Path(__file__).resolve().parent.parent / "core" / "app" / "static"


def check(cond, name, info=""):
    print(("  ✔ " if cond else "  ✖ ") + name + ("" if cond else f"  → {str(info)[:500]}"))
    if not cond:
        FAILS.append(name)


SEED = """
TRUNCATE families, users, user_identities, app_state, outbox, push_subscriptions RESTART IDENTITY CASCADE;
INSERT INTO families (name) VALUES ('Демо');
INSERT INTO users (family_id, name) VALUES (1,'Алексей'), (1,'Мария');
INSERT INTO user_identities (channel, external_id, user_id) VALUES ('telegram','111',1),('telegram','222',2);
"""
ANSWER = {"v": {"orgId": None, "userId": "6ae47642d9c2cd00011f2460", "object": "balance", "balance": 988.78, "creditLimit": 0}}


async def fake_get(url, params=None, as_="json", headers=None):
    CALLS.append((url, dict(headers or {})))
    v = ANSWER["v"]
    if isinstance(v, Exception):
        raise v
    return v


async def fake_send(uid, reply, topic=None, urgent=False, quiet="defer"):
    SENT.append((uid, reply.text, topic, urgent))
    return True


async def put(bal: float):
    ANSWER["v"] = {"userId": "u", "object": "balance", "balance": bal, "creditLimit": 0}
    return await balance.refresh()


async def main():
    db.pool = asyncpg.FakeConn()
    sql(SEED)
    net.get = fake_get
    real_send = notify.send
    cfg = get_settings()
    alex, maria = await users.get(1), await users.get(2)

    print("1. Подключён ли GPTunnel")
    check(balance.endpoint() == "https://gptunnel.ru/v1/balance", "адрес запроса баланса из LLM_BASE_URL", balance.endpoint())
    cfg.llm_provider = "anthropic"
    check(balance.endpoint() is None and not balance.enabled(), "провайдер не openai — баланса нет")
    cfg.llm_provider, cfg.llm_base_url = "openai", "https://openrouter.ai/api/v1"
    check(balance.endpoint() is None, "другой сервис (OpenRouter) — баланса нет")
    cfg.llm_base_url, cfg.llm_api_key = "https://gptunnel.ru/v1", ""
    check(balance.endpoint() is None, "ключа нет — баланса нет")
    cfg.llm_api_key = "sk-test-key"
    cfg.llm_base_url = "https://evilgptunnel.ru/v1"
    check(balance.endpoint() is None, "похожее имя сайта (evilgptunnel.ru) не подходит")
    cfg.llm_base_url = "https://gptunnel.ru/v1"

    print("2. Разбор ответа и расчёты")
    check(balance.parse(ANSWER["v"]) == {"balance": 988.78, "credit": 0.0}, "настоящий ответ GPTunnel", balance.parse(ANSWER["v"]))
    check(balance.parse({"balance": -51.54397, "creditLimit": 15000}) == {"balance": -51.54, "credit": 15000.0}, "отрицательный остаток и кредит")
    try:
        balance.parse({"error": {"message": "Unauthorized"}}); bad = False
    except ValueError:
        bad = True
    check(bad, "ответ без balance — ошибка")
    now = time.time()
    h = [[now - 3 * 86400, 1000.0], [now - 2 * 86400, 940.0], [now - 86400, 1240.0], [now, 1180.0]]   # расход 60+60, пополнение +300
    r = balance.rate_per_day(h, now)
    check(r == 40.0, "расход за сутки: пополнение в расход не входит (120 ₽ за 3 дня)", r)
    check(balance.rate_per_day([[now - 3600, 500.0], [now, 490.0]], now) is None, "меньше 6 часов наблюдений — расход не считаем")
    check(balance.rate_per_day([[now - 86400, 500.0], [now, 500.0]], now) is None, "расхода не было — None")
    check(balance.days_left(988.78, 40.0) == 24 and balance.days_left(0, 40.0) is None and balance.days_left(500, None) is None, "на сколько дней хватит")
    check([balance.level(b, 300) for b in (1000, 300, 299.99, 0.01, 0, -5)] == ["ok", "ok", "low", "low", "zero", "zero"], "уровни: ok / low / zero")

    print("3. Запрос и хранение")
    notify.send = fake_send
    check(await balance.refresh() is True, "refresh удался")
    check(CALLS[-1] == ("https://gptunnel.ru/v1/balance", {"Authorization": "Bearer sk-test-key"}), "запрос с ключом из LLM_API_KEY", CALLS[-1])
    rec = await balance._load()
    check(rec["data"] == {"balance": 988.78, "credit": 0.0} and len(rec["history"]) == 1, "остаток и история сохранены", rec)
    await balance.refresh()
    check(len((await balance._load())["history"]) == 1, "то же значение чаще раза в 30 минут в историю не пишем")
    ANSWER["v"] = RuntimeError("503")
    check(await balance.refresh() is False, "сбой сети — False")
    rec = await balance._load()
    check(rec["data"]["balance"] == 988.78 and rec["tried"] >= rec["at"], "после сбоя прежний остаток не потерян", rec)
    check(not balance._due(rec), "после сбоя повтор не раньше чем через 10 минут")
    rec["at"] = rec["tried"] = time.time() - 4000
    check(balance._due(rec), "через час снова пора обновлять")

    print("4. Уведомления владельцу")
    sql("TRUNCATE app_state"); SENT.clear()
    await put(988.78)
    check(not SENT, "много денег — тихо", SENT)
    await put(250)
    check(len(SENT) == 1 and SENT[0][0] == 1 and SENT[0][2] == "balance" and SENT[0][3] is False and "250 ₽" in SENT[0][1] and "300 ₽" in SENT[0][1],
          "ниже порога — одно сообщение владельцу (не Марии), не срочное", SENT)
    await put(240); await put(230)
    check(len(SENT) == 1, "пока остаток низкий, повторно в тот же день не пишем", len(SENT))
    st = await balance._load(balance.ALERT); st["1"]["at"] = time.time() - 25 * 3600; await balance._save(st, balance.ALERT)
    await put(220)
    check(len(SENT) == 2, "через сутки напоминаем снова")
    await put(-5)
    check(len(SENT) == 3 and SENT[-1][3] is True and "закончились" in SENT[-1][1], "остаток закончился — срочное сообщение", SENT[-1])
    await put(900); await put(250)
    check(len(SENT) == 4 and "заканчиваются" in SENT[-1][1], "после пополнения счётчик сброшен: снова упало — снова пишем", len(SENT))
    await prefs.update(alex, {"balance_low": 100})
    SENT.clear(); sql("TRUNCATE app_state"); await put(150)
    check(not SENT, "порог 100 ₽ — при 150 тихо")
    await put(90)
    check(len(SENT) == 1, "порог 100 ₽ — при 90 пишем", SENT)
    await prefs.update(alex, {"balance_low": 300})
    rec = await balance._load(); h = [[time.time() - 86400 * 2, 900.0], [time.time(), 820.0]]
    sql("TRUNCATE app_state"); await balance._save({"at": time.time(), "tried": time.time(), "data": {"balance": 250.0, "credit": 0.0}, "history": h})
    SENT.clear(); await balance.check_alerts(250.0)
    check(len(SENT) == 1 and "хватит примерно на 6 дн." in SENT[0][1], "в сообщении — на сколько дней хватит (40 ₽ в день)", SENT)
    notify.send = real_send
    await prefs.update(alex, {"notify": {"balance": False}})
    check(await real_send(1, balance.Reply(text="x"), topic="balance") is False, "тему «Баланс» можно выключить в настройках")
    await prefs.update(alex, {"notify": {"balance": True}})

    print("5. Панель и настройки")
    sql("TRUNCATE app_state")
    ANSWER["v"] = {"userId": "u", "object": "balance", "balance": 988.78, "creditLimit": 0}
    o = await web.balance_data(user=alex)
    check(o["enabled"] and o["balance"] == 988.78 and o["level"] == "ok" and o["low"] == 300 and o["days_left"] is None,
          "владелец: карточка с остатком (первый запрос подтягивает данные сразу)", o)
    check(await web.balance_data(user=maria) == {"enabled": False}, "не владелец баланс не видит")
    cfg.llm_provider = "anthropic"
    check(await web.balance_data(user=alex) == {"enabled": False}, "без GPTunnel карточка не показывается")
    cfg.llm_provider = "openai"
    s = await prefs.update(alex, {"balance_low": 500})
    check(s["balance_low"] == 500, "порог сохраняется", s.get("balance_low"))
    s = await prefs.update(alex, {"balance_low": "мусор"})
    check(s["balance_low"] == 300, "неверное значение — 300")
    s = await prefs.update(alex, {"balance_low": -10})
    check(s["balance_low"] == 300, "отрицательное — 300")
    s = await prefs.update(alex, {"balance_low": 0})
    check(s["balance_low"] == 0, "ноль — предупреждать только когда деньги закончились")
    await prefs.update(alex, {"balance_low": 300})
    check("balance" in prefs.TOPICS, "тема уведомлений есть в списке настроек")

    print("6. Фоновая задача")
    check("balance.refresh_due" in open(jobs.__file__).read(), "обновление баланса подключено к фоновым задачам")
    await balance.refresh_due()
    check(True, "refresh_due не падает")

    print("7. Значок и заставка")
    m = web.MANIFEST
    srcs = [i["src"] for i in m["icons"]]
    check(all(x.endswith("?v=3") for x in srcs) and len(srcs) == 3, "в манифесте новые адреса значков (с версией, чтобы обошёл кэш)", srcs)
    import struct

    def png(name):                      # размер и вид PNG из заголовка (без сторонних библиотек)
        b = (ROOT / f"{name}.png").read_bytes()
        assert b[:8] == b"\x89PNG\r\n\x1a\n", name
        w, h = struct.unpack(">II", b[16:24])
        return (w, h), b[25]            # b[25]: 2 — RGB, 6 — RGBA
    for name, size in (("icon-192", 192), ("icon-512", 512), ("icon-maskable-512", 512), ("apple-touch-icon", 180)):
        check(png(name)[0] == (size, size), f"{name}.png — {size}×{size}", png(name))
    check(png("icon-maskable-512")[1] == 2, "значок под обрезку — без прозрачности")
    check(web.STATIC.get("splash.jpg") == "image/jpeg" and (ROOT / "splash.jpg").stat().st_size < 150_000, "картинка заставки отдаётся и не тяжёлая")
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    check('id="splash"' in html and "function hideSplash" in html and "hideSplash();\n})();" in html and "display-mode: standalone" in html,
          "заставка в панели: только в установленном приложении, скрывается после загрузки")
    check(m["background_color"] == "#1f3358", "цвет фона системной заставки подобран под картинку")

    print("ИТОГ:", "всё прошло" if not FAILS else f"ошибок {len(FAILS)}: {FAILS}")


asyncio.run(main())
