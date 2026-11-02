"""Версия 1.9: телефоны — ключ устройства, здоровье из Health Connect, уведомления и SMS банков, баланс SIM."""
import _env  # noqa: F401  (пути и база)
import asyncio, json, subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import asyncpg
from app import ai, bankfeed, brain, db, device_api, devices, habits, health, notify, prefs, scope, simcards, users, web
from app.util import today

from _env import PSQL
sql = lambda s: subprocess.run(PSQL, input=s, capture_output=True, text=True, check=True).stdout.strip()
FAILS, SENT = [], []


def check(cond, name, info=""):
    print(("  ✔ " if cond else "  ✖ ") + name + ("" if cond else f"  → {str(info)[:500]}"))
    if not cond:
        FAILS.append(name)


async def fake_deliver(uid, reply):
    SENT.append((uid, reply.text, [b.action for r in reply.buttons for b in r if b.action]))
    return True


SEED = """
TRUNCATE families, users, user_identities, outbox, app_state, transactions, transaction_items, metrics, devices,
  bank_events, sim_cards, events RESTART IDENTITY CASCADE;
INSERT INTO families (name) VALUES ('Демо');
INSERT INTO users (family_id, name) VALUES (1,'Алексей'), (1,'Мария');
INSERT INTO user_identities (channel, external_id, user_id) VALUES ('telegram','111',1),('telegram','222',2);
"""

AI = {}


async def fake_json(system, text, **k):
    for key, val in AI.items():
        if key in text:
            return val
    if "|expense|" in text or "|income|" in text:          # категории для операций банка
        return {"rows": [{"i": 0, "category": "Продукты" if "PYATEROCHKA" in text.upper() else "Прочее",
                          "transfer": "Перевод между" in text}]}
    raise AssertionError("нет заготовки ИИ для: " + text[:80])


ms = lambda dt: int(dt.timestamp() * 1000)
NOW = datetime.now(timezone.utc)


async def main():
    ai.complete_json = fake_json
    db.pool = asyncpg.FakeConn()
    sql(SEED)
    notify.deliver = fake_deliver
    alex, maria = await users.get(1), await users.get(2)

    print("1. Разбор текстов банков")
    P = bankfeed.parse
    cases = [
        ("MIR-1234 14:05 Покупка 450р PYATEROCHKA Баланс: 12 345.67р", ("expense", "450", "12345.67", "1234", "PYATEROCHKA")),
        ("Покупка, карта *5678. 1 250,50 RUB. Пятёрочка. Доступно 9800 RUB", ("expense", "1250.50", "9800", "5678", "Пятёрочка")),
        ("Оплата 450р Карта*4321 MAGNIT Баланс 1000.00р 14:05", ("expense", "450", "1000.00", "4321", "MAGNIT")),
        ("Покупка 990,00 RUR. Карта ··8765 OZON. Остаток: 5 000,00 RUR", ("expense", "990.00", "5000.00", "8765", "OZON")),
        ("СЧЁТ1234 10:00 зачисление 50000р Зарплата Баланс: 62 345.67р", ("income", "50000", "62345.67", "1234", None)),
        ("Оплата 300 ₽ в «Кофе Хауз»", ("expense", "300", None, None, "Кофе Хауз")),
        ("Перевод 1000р от Иван И. Баланс: 2000р", ("income", "1000", "2000", None, None)),
    ]
    for text, (kind, amount, bal, tail, merchant) in cases:
        p = P(text)
        ok = p and p["kind"] == kind and p["amount"] == Decimal(amount) and (p["balance"] == (Decimal(bal) if bal else None)) \
            and p["card_tail"] == tail and (merchant is None or p["merchant"] == merchant)
        check(ok, f"разбор: {text[:45]}", p)
    check(P("Код подтверждения 1234. Никому не сообщайте") is None, "код — не храним")
    check(P("Оформите кредит со ставкой 5%! Одобрено 300 000 ₽") is None, "реклама — не храним")
    check(P("Покупка 450р отклонена: недостаточно средств") is None, "отказ — не операция")
    check(P("Перевод между своими счетами 5000р")["own"], "перевод между своими — отмечен")
    check(bankfeed.sanitize("Карта 2200 1234 5678 9012 списание") == "Карта ••9012 списание", "полный номер карты обрезан до 4 цифр",
          bankfeed.sanitize("Карта 2200 1234 5678 9012 списание"))
    check("+7•••" in bankfeed.sanitize("Перевод от +7 (914) 123-45-67"), "телефон скрыт")
    check(bankfeed.detect_bank("ru.sberbankmobile") == "sber" and bankfeed.detect_bank(sender="900", app="") == "sber"
          and bankfeed.detect_bank("x.y", label="Т-Банк") == "tbank" and bankfeed.detect_bank("com.whatsapp") is None,
          "банк по пакету, отправителю SMS и названию приложения")

    print("2. Ключ телефона")
    await scope.enter(alex)
    r = await device_api.devices_create({"name": "Xiaomi 13", "info": {"model": "2211133G", "app": "1.9.1", "evil": "x"}}, user=alex)
    key = r["key"]
    check(key.startswith("dev_") and len(key) > 30, "ключ выдан", r)
    check(sql("select count(*) from devices where key_hash like 'dev_%'") == "0", "в базе только хеш")
    dev = await device_api.device_auth(key)
    check(dev["user_id"] == 1 and dev["name"] == "Xiaomi 13", "телефон узнаётся по ключу", dev)
    info = json.loads(sql("select info from devices where id=1"))
    check(info.get("model") == "2211133G" and "evil" not in info, "лишние поля не сохраняются", info)
    try:
        await device_api.device_auth("dev_wrong"); bad = False
    except Exception as e:
        bad = "401" in str(getattr(e, "status_code", "")) or "не подходит" in str(e)
    check(bad, "чужой ключ — отказ")
    cfg = await device_api.device_config(device=dev)
    check(cfg["bank_on"] is False and "ru.sberbankmobile" in cfg["bank_packages"]["sber"] and cfg["ussd"][0]["code"] == "*100#",
          "настройки для телефона: банки выключены по умолчанию, списки и USSD", cfg)

    print("3. Здоровье")
    d0, d1 = today(), today() - timedelta(days=1)
    items = [{"kind": "steps", "day": d1.isoformat(), "value": 9500}, {"kind": "steps", "day": d0.isoformat(), "value": 3200},
             {"kind": "pulse", "day": d1.isoformat(), "value": 71.4, "min": 52, "max": 140},
             {"kind": "sleep", "start": ms(NOW - timedelta(hours=9)), "end": ms(NOW - timedelta(hours=1, minutes=30))},
             {"kind": "weight", "at": ms(NOW - timedelta(hours=2)), "value": 82.37},
             {"kind": "steps", "day": "2001-01-01", "value": 5}, {"kind": "pulse", "day": d0.isoformat(), "value": 400},
             {"kind": "smoke", "value": 1}]
    r = await device_api.device_health({"items": items}, device=dev)
    check(r == {"saved": 5, "skipped": 3}, "сохранено 5, отброшено 3 (старое, нереальное, неизвестное)", r)
    r = await device_api.device_health({"items": items[:2] + [{"kind": "steps", "day": d0.isoformat(), "value": 4100}]}, device=dev)
    check(sql("select count(*) from metrics where kind='steps'") == "2" and await habits.day_value(1, "steps", d0) == 4100,
          "повторная отправка не задваивает, шаги за сегодня обновились", sql("select kind,value,ext_id from metrics"))
    check(await habits.day_value(1, "sleep", d0) == Decimal("7.50"), "сон 7,5 ч за сегодня", await habits.day_value(1, "sleep", d0))
    check(sql("select extra->>'max' from metrics where kind='pulse'") == "140", "пульс: мин/макс сохранены")
    sql("UPDATE metrics SET deleted_at=now() WHERE kind='weight'")
    await device_api.device_health({"items": [items[4]]}, device=dev)
    check(sql("select count(*) from metrics where kind='weight' and deleted_at is null") == "0", "отменённая запись не воскресает")
    t = await habits.today_text(alex)
    check("❤️ Пульс" not in t or "в среднем" in t, "пульс в «привычках за сегодня» без нулей", t)
    hd = await web.habits_data(30, None, user=alex)
    check("pulse" in hd["series"] and hd["series"]["pulse"] and hd["units"]["pulse"] == "уд/мин", "панель: ряд пульса", hd["series"].get("pulse"))

    print("4. Банки: выключено по умолчанию")
    N = lambda text, app="ru.sberbankmobile", ch="push", at=None, sender="": {
        "channel": ch, "app": app, "sender": sender, "title": "", "text": text, "at": ms(at or NOW)}
    r = await device_api.device_bank({"items": [N("Покупка 450р PYATEROCHKA")]}, device=dev)
    check(r["off"] and sql("select count(*) from bank_events") == "0", "пока не включено — ничего не пишем", r)

    print("5. Банки: запись, дубли, остатки")
    await prefs.update(alex, {"bank_auto": True})
    check((await device_api.device_config(device=dev))["bank_on"], "переключатель в настройках включает")
    t1 = NOW - timedelta(minutes=30)
    SENT.clear()
    r = await device_api.device_bank({"items": [N("MIR-1234 Покупка 450р PYATEROCHKA Баланс: 12 345.67р", at=t1)]}, device=dev)
    check(r["stats"] == {"saved": 1}, "покупка записана", r)
    row = sql("select amount||'|'||source||'|'||merchant from transactions t join categories c on c.id=t.category_id where c.name='Продукты'")
    check(row == "450.00|bank_push|PYATEROCHKA", "трата: сумма, источник, магазин, категория", row)
    check(len(SENT) == 1 and "Из уведомления Сбер" in SENT[0][1] and "••1234" in SENT[0][1] and any(":del" in a for a in SENT[0][2]),
          "сообщение с кнопкой отмены", SENT)
    r = await device_api.device_bank({"items": [N("MIR-1234 Покупка 450р PYATEROCHKA Баланс: 12 345.67р", at=t1)]}, device=dev)
    check(r["stats"] == {"repeat": 1}, "то же уведомление второй раз — пропуск", r)
    r = await device_api.device_bank({"items": [N("MIR-1234 14:05 Покупка 450р PYATEROCHKA Баланс: 12345.67р", "", "sms",
                                                  t1 + timedelta(minutes=2), "900")]}, device=dev)
    check(r["stats"] == {"duplicate": 1} and sql("select count(*) from transactions where deleted_at is null") == "1",
          "та же покупка в SMS — дубль, трата одна", r)
    AI["такси 680"] = {"intent": "finance", "transactions": [{"kind": "expense", "amount": 680, "category": "Транспорт",
                                                               "date": today().isoformat()}]}
    await brain.handle_text(maria, "такси 680", source="text")
    r = await device_api.device_bank({"items": [N("Покупка, карта *5678. 680 RUB. YANDEX GO. Доступно 9800 RUB",
                                                  "com.idamob.tinkoff.android")]}, device=dev)
    check(r["stats"] == {"matched": 1} and sql("select count(*) from transactions where deleted_at is null") == "2",
          "уже записано вручную — только связали", r)
    r = await device_api.device_bank({"items": [N("Перевод между своими счетами 5000р. Баланс: 100р", at=NOW - timedelta(minutes=5)),
                                                N("Ваш код 4455 для входа"), N("Привет из WhatsApp", "com.whatsapp"),
                                                N("Непонятное сообщение банка про 3 рубля")]}, device=dev)
    check(r["stats"] == {"transfer": 1, "ignored": 2, "unparsed": 1}, "перевод себе, код, чужое приложение, непонятное", r)
    check(sql("select count(*) from bank_events where text like '%код%'") == "0", "коды не сохраняются")
    ov = await device_api.bankfeed_overview(user=alex)
    bal = {(b["bank"], b["card_tail"]): b["balance"] for b in ov["balances"]}
    check(bal.get(("sber", "1234")) == 12345.67 and bal.get(("tbank", "5678")) == 9800.0 and ov["me_on"],
          "остатки по картам", ov["balances"])

    print("6. Ручная запись после автоматической")
    r = await device_api.device_bank({"items": [N("Оплата 1200р Карта*4321 LENTA Баланс 1000.00р", "ru.vtb24.mobilebanking.android")]},
                                     device=dev)
    check(r["stats"] == {"saved": 1}, "ВТБ: записано автоматически", r)
    AI["лента 1200"] = {"intent": "finance", "transactions": [{"kind": "expense", "amount": 1200, "category": "Продукты",
                                                                "date": today().isoformat()}]}
    rep = await brain.handle_text(alex, "лента 1200", source="text")
    check("дубля" in rep.text and sql("select count(*) from transactions where amount=1200 and deleted_at is null") == "1"
          and sql("select source from transactions where amount=1200 and deleted_at is null") == "text",
          "ручная запись осталась, автоматическая убрана", rep.text)
    check(sql("select status from bank_events where amount=1200") == "matched", "уведомление связано с ручной записью")

    print("7. Баланс SIM")
    check(simcards.parse_balance("Баланс: 123,45 р.") == Decimal("123.45") and simcards.parse_balance("Ваш баланс минус 15.20 руб") == Decimal("-15.20")
          and simcards.parse_balance("Balance:1 234.00r") == Decimal("1234.00") and simcards.parse_balance("Сервис недоступен") is None,
          "разбор ответов операторов")
    check(simcards.operator("25002") == "МегаФон" and simcards.operator("", "Yota") == "Yota" and simcards.operator("25020") == "Т2",
          "оператор по коду сети и имени")
    SENT.clear()
    r = await device_api.device_sim({"items": [{"slot": 0, "mccmnc": "25002", "operator": "MegaFon", "text": "Баланс: 54,10 р."},
                                               {"slot": 1, "mccmnc": "25001", "operator": "MTS", "text": "Ваш баланс 420 руб"}]}, device=dev)
    check([s["balance"] for s in r["sims"]] == [54.1, 420.0], "баланс по двум SIM", r)
    check(len(SENT) == 1 and "МегаФон" in SENT[0][1] and "SIM 1" in SENT[0][1], "низкий баланс — одно напоминание", SENT)
    SENT.clear(); await device_api.device_sim({"items": [{"slot": 0, "mccmnc": "25002", "text": "Баланс: 40 р."}]}, device=dev)
    check(not SENT, "повторно не напоминает")
    await device_api.device_sim({"items": [{"slot": 0, "mccmnc": "25002", "text": "Баланс: 540 р."}]}, device=dev)
    SENT.clear(); await device_api.device_sim({"items": [{"slot": 0, "mccmnc": "25002", "text": "Баланс: 30 р."}]}, device=dev)
    check(len(SENT) == 1, "после пополнения и нового снижения — снова напомнит", SENT)
    r = await device_api.device_sim({"items": [{"slot": 1, "mccmnc": "25001", "text": "", "error": "Нет сети"}]}, device=dev)
    check(r["sims"][0]["error"] == "Нет сети" and sql("select balance from sim_cards where slot=1") == "420.00",
          "ошибка USSD — прежний баланс остаётся", r)

    print("8. Список и отзыв")
    lst = (await device_api.devices_list(user=alex))["devices"]
    check(len(lst) == 1 and lst[0]["mine"] and len(lst[0]["sims"]) == 2 and lst[0]["owner"] == "Алексей", "список телефонов семьи", lst)
    await scope.enter(maria)
    r2 = await device_api.devices_create({"name": "Xiaomi 14"}, user=maria)
    try:
        await device_api.devices_revoke(r2["id"] - 1, user=maria); denied = False
    except Exception:
        denied = True
    check(denied, "Мария не может отозвать телефон Алексея")
    await scope.enter(alex)
    r = await device_api.devices_revoke(r2["id"], user=alex)
    check(r["ok"], "владелец может отозвать телефон Марии")
    try:
        await device_api.device_auth(r2["key"]); gone = False
    except Exception:
        gone = True
    check(gone, "отозванный ключ больше не работает")
    scope.leave()

    print("9. Настройки")
    s = await prefs.update(alex, {"sim_low": "250", "bank_auto": False})
    check(s["sim_low"] == 250 and s["bank_auto"] is False and "bank" in prefs.TOPICS and "sim" in prefs.TOPICS, "порог и переключатель", s)
    s = await prefs.update(alex, {"sim_low": "abc"})
    check(s["sim_low"] == 250, "неверный порог не ломает")

    print("ИТОГ:", "всё прошло" if not FAILS else f"ошибок {len(FAILS)}: {FAILS}")


asyncio.run(main())
