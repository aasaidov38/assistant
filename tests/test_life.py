"""Версия 1.5: платежи, выписки, накопления, гараж, документы, счётчики, заметки, лекарства — на настоящей PostgreSQL."""
import _env  # noqa: F401  (пути и база)
import asyncio, base64, os, subprocess, sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
os.environ.update(DATABASE_URL="x", CORE_API_KEY="x", TZ="Asia/Irkutsk")
import asyncpg
from app import (ai, bank, bills, brain, db, garage, meds, meters, morning, notes, notify, papers, savings, trips, undo, users, util, web_life)
from app.schemas import Action, Inbound

from _env import PSQL
sql = lambda s: subprocess.run(PSQL, input=s, capture_output=True, text=True, check=True).stdout.strip()
Z = ZoneInfo("Asia/Irkutsk")
FAILS, SENT, AI = [], [], {}


def check(cond, name, info=""):
    print(("  ✔ " if cond else "  ✖ ") + name + ("" if cond else f"  → {str(info)[:400]}"))
    if not cond:
        FAILS.append(name)


async def fake_send(uid, reply, **kw):
    SENT.append((uid, reply.text, [b.action for r in reply.buttons for b in r]))
    return True


async def fake_json(system, text, **k):
    for key, val in AI.items():
        if key in text:
            return val(text) if callable(val) else val
    raise AssertionError("нет заготовки ИИ для: " + text[:80])


async def fake_complete(system, text, **k):
    if "Заметки семьи" in text:
        return "Размер обуви у младшей — 31 (записано 31.10.2026)."
    return "ok"


def set_now(dt):
    """Подменяет «сейчас» во всех модулях, в запросах и в значениях по умолчанию таблиц."""
    asyncpg.FAKE_NOW = dt
    cols = sql("select table_name||'.'||column_name from information_schema.columns where table_schema='public'"
               " and (column_default like 'now()%' or column_default like 'CURRENT_DATE%' or column_default like '%::timestamp with time zone')")
    stmts = []
    for c in cols.splitlines():
        t, col = c.split(".")
        typ = sql(f"select data_type from information_schema.columns where table_name='{t}' and column_name='{col}'")
        val = f"'{dt.date()}'::date" if typ == "date" else f"'{dt.isoformat()}'::timestamptz"
        stmts.append(f"ALTER TABLE {t} ALTER COLUMN {col} SET DEFAULT {val};")
    sql("\n".join(stmts))
    for mod in (util, bills, garage, meds, meters, papers, savings, morning, undo, notes, trips, web_life):
        if hasattr(mod, "now"):
            mod.now = lambda dt=dt: dt
        if hasattr(mod, "today"):
            mod.today = lambda dt=dt: dt.date()


SEED = """
TRUNCATE families, users, user_identities, transactions, transaction_items, meals, metrics, tasks, shopping_items, events,
  bills, bill_payments, bank_imports, bank_rows, savings_goals, savings_moves, vehicles, odometer, service_rules,
  service_log, season_sent, papers, attachments, meters, meter_readings, meter_reminders, family_notes, meds, med_intakes,
  trips, trip_places, trip_checklist
  RESTART IDENTITY CASCADE;
INSERT INTO families (name) VALUES ('Саидовы');
INSERT INTO users (family_id, name) VALUES (1,'Александр'), (1,'Яна');
INSERT INTO user_identities (channel, external_id, user_id) VALUES ('telegram','111',1),('telegram','222',2);
"""


async def say(user, text):
    return await brain.handle_text(user, text, source="text")


async def press(uid, action):
    return await brain.handle_action(Action(external_user_id=str(uid), action=action))


async def main():
    db.pool = asyncpg.FakeConn()
    sql(SEED)
    notify.send = fake_send
    ai.complete_json = fake_json
    ai.complete = fake_complete
    alex, yana = await users.get(1), await users.get(2)
    base_now = datetime(2026, 11, 3, 11, 0, tzinfo=Z)
    set_now(base_now)

    # ---------------- 1. Платежи ----------------
    print("1. Регулярные платежи")
    AI["кредит Сбер"] = {"intent": "bill", "bill": {"title": "Кредит Сбер", "amount": 15000, "category": "Кредиты", "day": 5, "period_months": 1, "remind_days": 3}}
    AI["ЖКХ каждое"] = {"intent": "bill", "bill": {"title": "ЖКХ", "amount": None, "category": "ЖКХ", "day": 20}}
    AI["КАСКО"] = {"intent": "bill", "bill": {"title": "КАСКО", "amount": 42000, "category": "Авто", "day": 15, "period_months": 12, "next_date": "2027-03-15"}}
    r = await say(alex, "кредит Сбер 15000 каждое 5 число")
    check("Кредит Сбер" in r.text and "05.11" in r.text, "платёж создан, срок 05.11", r.text)
    await say(alex, "ЖКХ каждое 20 число"); await say(alex, "КАСКО 42000 раз в год 15 марта")
    check(sql("select next_due from bills where title='КАСКО'") == "2027-03-15", "годовой платёж: срок 15.03.2027")
    SENT.clear(); await bills.send_due()
    check(len(SENT) == 1 and "Кредит Сбер" in SENT[0][1] and SENT[0][0] == 1, "напоминание за 2 дня — только владельцу", SENT)
    SENT.clear(); await bills.send_due()
    check(not SENT, "второй раз в тот же день не напоминает")
    set_now(base_now + timedelta(days=1)); SENT.clear(); await bills.send_due()
    check(not SENT, "на следующий день (до срока) тишина", SENT)
    set_now(base_now + timedelta(days=2)); SENT.clear(); await bills.send_due()
    check(len(SENT) == 1 and "Сегодня платёж" in SENT[0][1], "в день срока — напоминание", SENT)
    pay_action = SENT[0][2][0]
    r = await press(111, pay_action)
    check("Оплачено" in r.text and "05.12.2026" in r.text, "кнопка «Оплачено»: трата и следующий срок", r.text)
    check(sql("select count(*) from transactions where source='bill' and deleted_at is null") == "1", "записана трата")
    tx = sql("select id from transactions where source='bill'")
    op, _ = await undo.cancel(alex, f"t.{tx}")
    check(sql("select next_due from bills where title='Кредит Сбер'") == "2026-11-05", "отмена траты возвращает срок платежа")
    await undo.restore(alex, f"t.{tx}")
    check(sql("select next_due from bills where title='Кредит Сбер'") == "2026-12-05", "возврат траты снова переносит срок")
    AI["оплатил ЖКХ"] = {"intent": "bill_paid", "bill_paid": {"title": "ЖКХ", "amount": 6230}}
    r = await say(alex, "оплатил ЖКХ 6230")
    check("6 230 ₽" in r.text, "«оплатил ЖКХ 6230» по названию", r.text)
    r = await say(alex, "/bills")
    check("Регулярные платежи" in r.text and "КАСКО" in r.text, "/bills список", r.text)

    # ---------------- 2. Выписки ----------------
    print("2. Импорт выписки (Т-Банк CSV)")
    sql("""INSERT INTO transactions (family_id,user_id,kind,amount,category_id,merchant,occurred_on,source)
           VALUES (1,1,'expense',1450,(select id from categories where name='Продукты'),'Пятёрочка','2026-10-28','text')""")
    csv_text = ('"Дата операции";"Дата платежа";"Номер карты";"Статус";"Сумма операции";"Валюта операции";"Сумма платежа";'
                '"Валюта платежа";"Кэшбэк";"Категория";"MCC";"Описание";"Бонусы (включая кэшбэк)"\n'
                '"28.10.2026 18:22:10";"28.10.2026";"*1234";"OK";"-1450,00";"RUB";"-1450,00";"RUB";"";"Супермаркеты";"5411";"Пятёрочка";"14,00"\n'
                '"29.10.2026 09:10:00";"29.10.2026";"*1234";"OK";"-320,00";"RUB";"-320,00";"RUB";"";"Кафе";"5814";"Кофейня Мята";"3,00"\n'
                '"29.10.2026 12:00:00";"29.10.2026";"*1234";"FAILED";"-999,00";"RUB";"-999,00";"RUB";"";"Разное";"";"Отклонено";""\n'
                '"30.10.2026 10:00:00";"30.10.2026";"*1234";"OK";"-5000,00";"RUB";"-5000,00";"RUB";"";"Переводы";"";"Перевод между своими счетами";""\n'
                '"31.10.2026 15:00:00";"31.10.2026";"*1234";"OK";"2500,00";"RUB";"2500,00";"RUB";"";"Пополнения";"";"Возврат за товар";""\n')
    AI["Кофейня Мята"] = {"rows": [{"i": 0, "category": "Кафе и рестораны"}, {"i": 1, "category": "Прочее", "transfer": True},
                                    {"i": 2, "category": "Возврат"}]}
    r = await brain.handle_inbound(Inbound(external_user_id="111", kind="document", filename="tinkoff_oct.csv",
                                           mime="text/csv", file_b64=base64.b64encode(csv_text.encode("cp1251")).decode()))
    check("Т-Банк" in r.text and "уже записаны: 1" in r.text and "новых: <b>2</b>" in r.text, "сверка: 1 уже есть, 2 новых, 1 перевод, отклонённая пропущена", r.text)
    check("Переводы между своими" in r.text, "перевод между своими счетами не добавляется", r.text)
    AI["0|expense|5000.00|Перевод"] = {"rows": [{"i": 0, "category": "Прочее", "transfer": True}]}
    act = next(a for row in r.buttons for a in [row[0].action] if a.endswith(":all"))
    r = await press(111, act)
    check("Добавил 2 операции" in r.text, "«Добавить все» добавила 2", r.text)
    check(sql("select count(*) from transactions where source='bank'") == "2", "в базе 2 операции из выписки")
    r = await brain.handle_inbound(Inbound(external_user_id="111", kind="document", filename="tinkoff_oct.csv",
                                           mime="text/csv", file_b64=base64.b64encode(csv_text.encode()).decode()))
    check("новых: <b>0</b>" in r.text or "Всё из выписки уже записано" in r.text, "повторная загрузка той же выписки — новых нет", r.text)
    r = await brain.handle_inbound(Inbound(external_user_id="111", kind="document", filename="x.xlsx", mime="", file_b64="AA=="))
    check("CSV" in r.text, "Excel — понятная просьба прислать CSV/PDF", r.text)

    # ---------------- 3. Накопления ----------------
    print("3. Накопления")
    AI["копим на отпуск"] = {"intent": "savings_goal", "savings_goal": {"title": "Отпуск", "target": 150000, "deadline": "2027-07-01"}}
    AI["отложил на отпуск"] = {"intent": "savings_move", "savings_move": {"goal": "отпуск", "amount": 10000}}
    r = await say(alex, "копим на отпуск 150000 к июлю")
    check("Отпуск" in r.text and "в месяц" in r.text, "цель создана, считает ежемесячный взнос", r.text)
    r = await say(yana, "отложил на отпуск 10000")
    check("10 000 ₽ из 150 000 ₽" in r.text, "пополнение от Яны", r.text)
    r = await press(111, next(b.action for row in r.buttons for b in row if b.action.endswith(":add:5000")))
    check("15 000 ₽" in r.text, "кнопка +5000", r.text)
    mv = sql("select max(id) from savings_moves")
    await undo.cancel(alex, f"v.{mv}")
    check("10 000 ₽" in (await savings.list_reply(alex)).text, "отмена пополнения через историю")

    # ---------------- 4. Гараж ----------------
    print("4. Гараж")
    AI["добавь мотоцикл"] = {"intent": "vehicle", "vehicle": {"name": "Honda CB400", "kind": "moto", "km": 21000}}
    AI["пробег мото"] = {"intent": "odometer", "odometer": {"vehicle": "мото", "km": 27500}}
    AI["заправил мото"] = {"intent": "finance", "transactions": [{"kind": "expense", "amount": 1500, "category": "Топливо", "date": "2026-11-03", "vehicle": "мото"}]}
    AI["менял масло"] = {"intent": "service_done", "service_done": {"vehicle": "мото", "title": "масло двигателя", "km": 27600, "cost": 4500}}
    AI["ОСАГО на мото"] = {"intent": "paper", "paper": {"kind": "policy", "title": "ОСАГО", "vehicle": "мото", "expires_on": "2026-11-20"}}
    r = await say(alex, "добавь мотоцикл Honda CB400, пробег 21000")
    check("Honda CB400" in r.text and r.buttons, "мотоцикл добавлен, предложен регламент", r.text)
    r = await press(111, r.buttons[0][0].action)
    check("Регламент" in r.text and "Замена масла" in r.text, "типовой регламент добавлен", r.text)
    await say(alex, "заправил мото 1500")
    check(sql("select count(*) from transactions where vehicle_id=1") == "1", "заправка привязана к мотоциклу")
    r = await say(alex, "пробег мото 27500")
    check("27 500 км" in r.text and "Замена масла" in r.text, "пробег 27500 → масло просрочено (6000 км с 21000)", r.text)
    SENT.clear(); await garage.send_due()
    check(any("Пора на ТО" in t for _, t, _ in SENT), "напоминание о ТО", SENT)
    r = await say(alex, "менял масло в мото на 27600, 4500 р")
    check("Замена масла двигателя" in r.text and "33 600 км" in r.text and "4 500 ₽" in r.text, "ТО записано, следующий раз на 33600, трата 4500", r.text)
    log_id = sql("select max(id) from service_log")
    await undo.cancel(alex, f"l.{log_id}")
    check(sql("select last_km from service_rules where title='Замена масла двигателя'") == "21000"
          and sql(f"select deleted_at is not null from transactions where id=(select transaction_id from service_log where id={log_id})") == "t",
          "отмена ТО: регламент и трата откатились")
    await undo.restore(alex, f"l.{log_id}")
    check(sql("select last_km from service_rules where title='Замена масла двигателя'") == "27600", "возврат ТО")
    await say(alex, "ОСАГО на мото до 20.11.2026")
    r = await say(alex, "/garage")
    check("ОСАГО" in r.text and "Honda" in r.text, "/garage: техника, регламент, страховка", r.text)
    set_now(datetime(2026, 10, 2, 11, 0, tzinfo=Z)); SENT.clear(); await garage.send_due()
    check(any("Консервация мотоцикла" in t for _, t, _ in SENT), "сезон: консервация мотоцикла в октябре", [t[:40] for _, t, _ in SENT])
    SENT.clear(); await garage.send_due()
    check(not any("Консервация" in t for _, t, _ in SENT), "сезонное — один раз в год")
    set_now(base_now)

    # ---------------- 5. Документы ----------------
    print("5. Документы и гарантии")
    AI["паспорт Яны"] = {"intent": "paper", "paper": {"kind": "document", "title": "Паспорт", "owner": "Яна", "expires_on": "2027-01-15"}}
    AI["ОСАГО продлил"] = {"intent": "paper", "paper": {"kind": "policy", "title": "ОСАГО на мотоцикл", "vehicle": "мото", "expires_on": "2027-11-20", "renew": True}}
    r = await say(alex, "паспорт Яны до 2027-01-15")
    check("Паспорт" in r.text and "Яна" in r.text, "документ сохранён", r.text)
    SENT.clear(); await papers.send_due()
    check(any("ОСАГО" in t and "17 дн" in t for _, t, _ in SENT) and any("Паспорт" in t for _, t, _ in SENT),
          "напоминания: ОСАГО через 17 дн., паспорт через 73 дн.", [t[:60] for _, t, _ in SENT])
    SENT.clear(); await papers.send_due()
    check(not SENT, "повторно по тому же порогу не шлёт")
    r = await say(alex, "ОСАГО продлил до 20.11.2027")
    check("Обновил срок" in r.text and sql("select count(*) from papers where kind='policy'") == "1", "продление обновляет, а не дублирует", r.text)
    AI["Разбери фото"] = {"type": "warranty", "title": "Гарантия: холодильник Bosch", "purchased_on": "2026-03-12", "warranty_months": 24}
    r = await brain.handle_photo(alex, base64.b64encode(b"\xff\xd8fakejpg").decode(), "image/jpeg", "гарантия")
    check("Гарантия" in r.text and "12.03.2028" in r.text and sql("select count(*) from attachments") == "1", "фото гарантии: срок 24 мес., фото сохранено", r.text)
    AI["запомни пароль"] = {"intent": "note_save", "note_save": {"subject": "wifi", "text": "пароль от wifi qwerty123"}}
    r = await say(alex, "запомни пароль от wifi qwerty123")
    check("не сохраняю" in r.text, "пароль не сохраняется даже если ИИ ошибся", r.text)

    # ---------------- 6. Счётчики ----------------
    print("6. Счётчики")
    AI["счётчик холодная"] = {"intent": "meter_add", "meter_add": {"title": "Холодная вода кухня", "kind": "cold_water", "verify_until": "2026-12-01"}}
    AI["счётчик горячая"] = {"intent": "meter_add", "meter_add": {"title": "Горячая вода кухня", "kind": "hot_water"}}
    AI["холодная вода 123"] = {"intent": "meter_reading", "meter_reading": [{"meter": "Холодная вода кухня", "kind": "cold_water", "value": 123.456}]}
    await say(alex, "счётчик холодная вода кухня, поверка до 2026-12-01"); await say(alex, "счётчик горячая вода кухня")
    sql("insert into meter_readings (family_id, meter_id, value, created_at) values (1,1,118.2,'2026-10-21'),(1,2,40.1,'2026-10-21')")
    set_now(datetime(2026, 11, 20, 11, 0, tzinfo=Z)); SENT.clear(); await meters.send_due()
    check(any("Пора передать показания" in t for _, t, _ in SENT), "20-го: просьба передать показания", SENT)
    check(any("Поверка" in t for _, t, _ in SENT), "поверка через 11 дней — напоминание", [t[:50] for _, t, _ in SENT])
    r = await say(alex, "холодная вода 123,456")
    check("123,456" in r.text and "+5,256" in r.text, "показание и расход за месяц", r.text)
    AI["Разбери фото"] = {"type": "meter", "kind": "hot_water", "value": 45.2}
    r = await brain.handle_photo(alex, base64.b64encode(b"img").decode(), "image/jpeg", "")
    check("Горячая вода кухня: <b>45,2</b>" in r.text and "Все показания за ноябрь собраны" in r.text, "фото счётчика + сводка за месяц", r.text)
    sql("insert into meters (family_id, user_id, title, kind) values (1,1,'Холодная вода ванная','cold_water')")
    AI["Разбери фото"] = {"type": "meter", "kind": "cold_water", "value": 50}
    r = await brain.handle_photo(alex, base64.b64encode(b"img").decode(), "image/jpeg", "")
    check("чей это счётчик" in r.text and len(r.buttons) == 2, "два счётчика холодной воды — кнопки выбора", r.text)
    r = await press(111, r.buttons[1][0].action)
    check("ванная" in r.text and "50" in r.text, "выбор счётчика кнопкой", r.text)
    set_now(base_now)

    # ---------------- 7. Заметки ----------------
    print("7. Семейные заметки")
    AI["размер обуви у младшей 31"] = {"intent": "note_save", "note_save": {"subject": "младшая", "text": "размер обуви 31"}}
    AI["какой размер обуви"] = {"intent": "note_ask", "note_ask": {"question": "какой размер обуви у младшей?"}}
    r = await say(alex, "запомни: размер обуви у младшей 31")
    check("Запомнил" in r.text and "размер обуви 31" in r.text, "заметка сохранена", r.text)
    r = await say(yana, "какой размер обуви у младшей?")
    check("31" in r.text, "ответ по заметкам", r.text)
    r = await say(alex, "/notes обувь")
    check("размер обуви 31" in r.text, "поиск по заметкам", r.text)

    # ---------------- 8. Лекарства ----------------
    print("8. Лекарства")
    AI["витамин D по 1"] = {"intent": "med_add", "med_add": {"title": "Витамин D", "dose": "1 таблетка", "times": ["09:00", "21:00"],
                                                        "stock": 10, "per_intake": 1, "unit": "шт"}}
    AI["принял витамин"] = {"intent": "med_taken", "med": {"name": "витамин D"}}
    r = await say(alex, "витамин D по 1 таблетке в 9:00 и 21:00, в упаковке 10")
    check("Витамин D" in r.text and "09:00, 21:00" in r.text and "≈ 5 дн." in r.text, "лекарство добавлено, остаток на 5 дней", r.text)
    set_now(datetime(2026, 11, 3, 9, 1, tzinfo=Z)); SENT.clear(); await meds.send_due()
    check(len(SENT) == 1 and "Время принять" in SENT[0][1] and SENT[0][0] == 1, "в 9:01 — напоминание", SENT)
    take = SENT[0][2][0]
    SENT.clear(); await meds.send_due()
    check(not SENT, "повторно сразу не шлёт")
    r = await press(111, take)
    check("Принято" in r.text and "заканчивается" in r.text and any("shop" in b.action for row in r.buttons for b in row),
          "«Принял»: остаток 9 → ≈4 дня → предложение купить", r.text)
    r = await press(111, next(b.action for row in r.buttons for b in row if "shop" in b.action))
    check("Витамин D" in r.text and sql("select dept from shopping_items where title='Витамин D'") == "аптека", "в список покупок (отдел «аптека»)", r.text)
    set_now(datetime(2026, 11, 3, 21, 0, 30, tzinfo=Z)); SENT.clear(); await meds.send_due()
    iid = sql("select max(id) from med_intakes")
    sql(f"update med_intakes set last_sent_at = now() - interval '50 minutes' where id={iid}")
    SENT.clear(); await meds.send_due()
    check(any("ещё раз" in t for _, t, _ in SENT), "нет ответа 45 минут — повтор", SENT)
    r = await say(alex, "принял витамин D")
    check("Принято" in r.text and sql(f"select status from med_intakes where id={iid}") == "taken", "«принял витамин D» закрывает текущий приём", r.text)
    r = await press(111, f"x:i{iid}:untake")
    check(sql(f"select status from med_intakes where id={iid}") == "pending" and sql("select stock from meds") == "9.00",
          "«Не принимал» возвращает остаток", sql("select stock from meds"))

    # ---------------- 13. Поездки ----------------
    print("13. Поездки")
    AI["поездка на Байкал"] = {"intent": "trip", "trip": {"title": "Байкал", "kind": "moto", "start_on": "2026-11-05",
                               "end_on": "2026-11-08", "budget": 30000, "route": ["Иркутск", "Листвянка"], "extra_items": ["термос"]}}
    AI["в сборы"] = {"intent": "trip_items", "trip_items": ["дождевик для детей"]}
    AI["кафе на Байкале"] = {"intent": "finance", "transactions": [{"kind": "expense", "amount": 2400, "category": "Кафе и рестораны", "date": "2026-11-06"}]}
    sql("insert into transactions (family_id,user_id,kind,amount,category_id,occurred_on,source) values "
        "(1,1,'expense',8000,(select id from categories where name='Прочее'),'2026-11-05','text')")
    r = await say(alex, "поездка на Байкал с 5 по 8 ноября на мото, бюджет 30000, маршрут Иркутск — Листвянка")
    check("Байкал" in r.text and "4 дня" in r.text and "12 500 ₽ из 30 000 ₽" in r.text and "через 2 дня" in r.text,
          "поездка создана, траты в её даты уже засчитаны (8000 + ТО 4500 от 05.11; оплата кредита — нет)", r.text)
    check(sql("select count(*) from trip_checklist") == str(len(trips.CHECKLIST["moto"]) + len(trips.CHECKLIST["common"]) + 1),
          "чек-лист: мото + общий + свой пункт")
    r = await say(alex, "в сборы дождевик для детей")
    check("дождевик для детей" in r.text, "пункт добавлен в сборы", r.text[:200])
    r = await press(111, r.buttons[0][0].action)
    check(r.text.count("✅") >= 1 and "1 из" in r.text, "отметка пункта кнопкой", r.text[:120])
    SENT.clear(); await trips.send_due()
    check(any("Поездка через 2 дн." in t for _, t, _ in SENT), "напоминание за 3 дня", [t[:60] for _, t, _ in SENT])
    SENT.clear(); await trips.send_due()
    check(not SENT, "повторно не шлёт")
    set_now(datetime(2026, 11, 6, 12, 0, tzinfo=Z))
    r = await say(alex, "кафе на Байкале 2400")
    check(sql("select count(*) from transactions where trip_id=1") == "3", "трата в дни поездки относится к ней сама")
    r = await say(alex, "/trip")
    check("день 2 из 4" in r.text and "14 900 ₽ из 30 000 ₽" in r.text and "5 033 ₽ в день" in r.text, "/trip: статус, бюджет, в день", r.text)
    lst = await web_life.trips_list(user=alex)
    check(lst[0]["spent"] == 14900 and len(lst[0]["daily"]) == 2 and lst[0]["places"][0]["name"] == "Иркутск", "панель: список поездок", lst[0]["spent"])
    dash = await web_life.dashboard(user=alex)
    check(len(dash["spend30"]) == 30 and dash["trip"]["title"] == "Байкал" and len(dash["kcal14"]) == 14, "панель: сводка для главной", list(dash))
    set_now(base_now)

    # ---------------- Общее ----------------
    print("Общее")
    set_now(datetime(2026, 11, 3, 7, 30, tzinfo=Z))
    morning.weather = lambda: asyncio.sleep(0, result="")
    r = await morning.build(alex)
    check("💊 Сегодня: 21:00 Витамин D" in r.text and "🔧" in r.text, "утренняя сводка: лекарства и гараж", r.text[-500:])
    ops = await undo.history(1, days=60)
    types = {o["type"] for o in ops}
    check({"savings", "garage", "paper", "meter", "note", "finance"} <= types, "история видит новые типы записей", types)
    check(all(len(b.encode()) <= 64 for _, _, acts in SENT for b in acts), "все кнопки ≤ 64 байт")
    r = await say(alex, "/help")
    check("/bills" in r.text and "/meds" in r.text and len(r.text) < 4096, "справка с новыми командами и влезает в сообщение", len(r.text))

    print("\nИТОГ:", "всё прошло" if not FAILS else f"ошибок: {len(FAILS)} → {FAILS}")


asyncio.run(main())
