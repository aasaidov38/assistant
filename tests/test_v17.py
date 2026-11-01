"""Версия 1.7: долги, настройки, тихие часы, личные и общие разделы — на настоящей PostgreSQL."""
import _env  # noqa: F401  (пути и база)
import asyncio, os, subprocess, sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
os.environ.update(DATABASE_URL="x", CORE_API_KEY="x", TZ="Asia/Irkutsk")
import asyncpg
from fastapi import HTTPException
from app import (ai, analytics, bills, brain, budgets, db, debts, digest, finance, morning, notes, notify, prefs, scope,
                 shopping, undo, users, util, web, web_life)
from app.schemas import Action, Reply

from _env import PSQL
sql = lambda s: subprocess.run(PSQL, input=s, capture_output=True, text=True, check=True).stdout.strip()
Z = ZoneInfo("Asia/Irkutsk")
FAILS, SENT, AI = [], [], {}


def check(cond, name, info=""):
    print(("  ✔ " if cond else "  ✖ ") + name + ("" if cond else f"  → {str(info)[:500]}"))
    if not cond:
        FAILS.append(name)


async def fake_deliver(uid, reply):
    SENT.append((uid, reply.text, [b.action for r in reply.buttons for b in r]))
    return True


async def fake_json(system, text, **k):
    for key, val in AI.items():
        if key in text:
            return val(text) if callable(val) else val
    raise AssertionError("нет заготовки ИИ для: " + text[:80])


async def fake_complete(system, text, **k):
    return "ok"


def set_now(dt):
    asyncpg.FAKE_NOW = dt
    for mod in (util, debts, digest, morning, prefs, notify, bills, web_life, undo):
        if hasattr(mod, "now"):
            mod.now = lambda dt=dt: dt
        if hasattr(mod, "today"):
            mod.today = lambda dt=dt: dt.date()


SEED = """
TRUNCATE families, users, user_identities, transactions, transaction_items, meals, metrics, metric_goals, tasks, shopping_items,
  events, bills, bill_payments, savings_goals, savings_moves, family_notes, meds, med_intakes, budgets, budget_alerts,
  debts, debt_moves, outbox, integrations
  RESTART IDENTITY CASCADE;
INSERT INTO families (name) VALUES ('Саидовы');
INSERT INTO users (family_id, name) VALUES (1,'Александр'), (1,'Яна');
INSERT INTO user_identities (channel, external_id, user_id) VALUES ('telegram','111',1),('telegram','222',2);
"""


async def say(user, text):
    return await brain.handle_text(user, text, source="text")


async def press(uid, action):
    return await brain.handle_action(Action(external_user_id=str(uid), action=action))


async def ro(uid, q):
    """Как analytics.run_sql: роль assistant_ro, семья 1, спрашивает uid."""
    out = sql(f"BEGIN; SELECT set_config('app.family_id','1',true); SELECT set_config('app.user_id','{uid}',true);"
              f" SET LOCAL ROLE assistant_ro; COPY ({q}) TO STDOUT; COMMIT;")
    return [l.split("\t") for l in out.splitlines() if l and l not in ("1", str(uid))]


async def as_(u):
    """То, что делает зависимость current_user в панели."""
    await scope.enter(u)
    return u


async def main():
    db.pool = asyncpg.FakeConn()
    sql(SEED)
    notify.deliver = fake_deliver
    ai.complete_json = fake_json
    ai.complete = fake_complete
    alex, yana = await users.get(1), await users.get(2)
    base = datetime(2026, 11, 3, 11, 0, tzinfo=Z)
    set_now(base)

    # ---------------- Долги ----------------
    print("1. Долги")
    AI["в долг 5000"] = {"intent": "debt", "debt": {"direction": "lent", "person": "Петя", "amount": 5000, "due": "2026-11-15"}}
    r = await say(alex, "дал Пете в долг 5000 до 15 ноября")
    check("Петя" in r.text and "5 000 ₽" in r.text and "e:1:all" in [b.action for row in r.buttons for b in row],
          "«дал Пете в долг 5000 до 15 ноября»: долг и кнопки", r.text)
    check(sql("select count(*) from transactions") == "0", "долг — не трата")
    AI["вернул 2000"] = {"intent": "debt_repay", "debt_repay": {"person": "Пети", "amount": 2000, "direction": "lent"}}
    r = await say(alex, "Петя вернул 2000")
    check("Вернули 2 000 ₽" in r.text and "3 000 ₽" in r.text, "частичный возврат: остаток 3000", r.text)
    AI["ещё 1000"] = {"intent": "debt", "debt": {"direction": "lent", "person": "Петя", "amount": 1000}}
    r = await say(alex, "дал Пете ещё 1000")
    check("Добавил к долгу" in r.text and "4 000 ₽" in r.text and sql("select count(*) from debts") == "1",
          "ещё раз тому же человеку — добавляется к долгу", r.text)
    AI["занял у Ивана"] = {"intent": "debt", "debt": {"direction": "borrowed", "person": "Иван", "amount": 3000, "due": "2026-11-06"}}
    r = await say(alex, "занял у Ивана 3000 до 6 ноября")
    check("вы должны" in r.text and "за 3 дня" in r.text, "«занял у Ивана»: я должен", r.text)
    r = await say(alex, "/debts")
    check("Мне должны: 4 000 ₽" in r.text and "Я должен: 3 000 ₽" in r.text, "/debts: итоги в обе стороны", r.text)

    print("   напоминания")
    SENT.clear(); await debts.send_due()
    check(len(SENT) == 1 and SENT[0][0] == 1 and "Через 3 дн." in SENT[0][1] and "Иван" in SENT[0][1],
          "за 3 дня до срока «я должен» — напоминание автору", SENT)
    SENT.clear(); await debts.send_due()
    check(not SENT, "повторно в тот же день — тишина", SENT)
    set_now(base + timedelta(days=3)); SENT.clear(); await debts.send_due()
    check(len(SENT) == 1 and "Сегодня срок" in SENT[0][1], "в день срока", SENT)
    r = await press(111, "e:2:later")
    check("Напомню 13.11" in r.text, "кнопка «Через неделю»", r.text)
    set_now(base + timedelta(days=5)); SENT.clear(); await debts.send_due()
    check(not SENT, "отложено — не напоминает", SENT)
    set_now(base + timedelta(days=10)); SENT.clear(); await debts.send_due()
    check(len(SENT) == 1 and "просрочен" in SENT[0][1], "после отсрочки — про просрочку", SENT)
    set_now(base + timedelta(days=12)); SENT.clear(); await debts.send_due()
    check(len(SENT) == 1 and "Петя" in SENT[0][1] and "обещал" in SENT[0][1], "«мне должны» — в день срока", SENT)
    r = await press(111, "e:2:all")
    check("Долг закрыт" in r.text, "кнопка «Я вернул всё» закрывает долг", r.text)
    set_now(base + timedelta(days=12, hours=1)); SENT.clear(); await debts.send_due()
    check(not any("Иван" in t for _, t, _ in SENT), "по закрытому долгу не напоминает", SENT)
    set_now(base)

    print("   отмена")
    mid = sql("select max(id) from debt_moves")
    op, _ = await undo.cancel(alex, f"p.{mid}")
    check(op and op["cancelled"] and len(await debts.debts(1)) == 2, "отмена возврата снова открывает долг", op)
    await undo.restore(alex, f"p.{mid}")
    h = await undo.history(1, types=["debt"])
    check(any(o["ref"] == "e.1" for o in h) and any(o["ref"].startswith("p.") for o in h), "долги в истории", h[:3])
    AI["отмени последний долг"] = {"intent": "undo", "undo": {"what": "debt"}}
    r = await say(alex, "отмени последний долг")
    check("Отменено" in r.text or "🗑" in r.text, "«отмени последний долг»", r.text)
    sql("UPDATE debt_moves SET deleted_at=NULL; UPDATE debts SET deleted_at=NULL")

    print("   панель")
    await as_(alex)
    j = await web_life.debts_list(closed=1, user=alex)
    p = next(d for d in j["debts"] if d["person"] == "Петя")
    check(j["totals"]["lent"] == 4000 and len(p["moves"]) == 2 and p["ref"] == "e.1", "GET /debts: итоги и движения", j["totals"])
    r = await web_life.debts_repay(1, web_life.DebtMoveIn(amount=None), user=alex)
    check("Долг закрыт" in r["text"], "панель: «вернули всё»", r)
    r = await web_life.debts_add(web_life.DebtIn(direction="lent", person="Серёжа", amount=1500), user=alex)
    check("Серёжа" in r["text"], "панель: новый долг", r)

    print("   вопросы к данным (личный раздел)")
    rows = await ro(1, "SELECT person, balance FROM ro.debts")
    check(len(rows) >= 2, "Александр видит свои долги в ro.debts", rows)
    rows = await ro(2, "SELECT person FROM ro.debts")
    check(rows == [], "Яна не видит долги Александра (раздел личный)", rows)
    await as_(yana)
    check(await debts.debts(1) == [], "в боте Яна тоже не видит чужие долги")
    await as_(alex)

    # ---------------- Личные и общие разделы ----------------
    print("2. Личные и общие разделы")
    m = await scope.modes(1)
    check(m["finance"] == "shared" and m["nutrition"] == "personal" and m["debts"] == "personal", "режимы по умолчанию", m)
    AI["Пятёрочка 1000"] = {"intent": "finance", "transactions": [{"kind": "expense", "amount": 1000, "category": "Продукты", "date": "2026-11-03"}]}
    AI["Кофейня 300"] = {"intent": "finance", "transactions": [{"kind": "expense", "amount": 300, "category": "Кафе и рестораны", "date": "2026-11-03"}]}
    await say(alex, "Пятёрочка 1000"); await say(yana, "Кофейня 300")
    await as_(yana)
    f = await web.finance_data(month="2026-11", user=yana)
    check(f["expense"] == 1300, "траты общие: Яна видит 1300", f["expense"])
    try:
        await scope.set_modes(yana, {"finance": "personal"}); check(False, "не владелец не меняет режим")
    except PermissionError:
        check(True, "не владелец не меняет режим")
    try:
        await web_life.sections_save({"finance": "personal"}, user=yana); check(False, "панель: 403 не владельцу")
    except HTTPException as e:
        check(e.status_code == 403, "панель: 403 не владельцу")
    await as_(alex)
    await web_life.sections_save({"finance": "personal", "shopping": "personal", "nutrition": "shared", "xxx": "personal"}, user=alex)
    m = await scope.modes(1)
    check(m["finance"] == "personal" and m["nutrition"] == "shared" and "xxx" not in m, "владелец переключил разделы", m)
    await as_(yana)
    f = await web.finance_data(month="2026-11", user=yana)
    check(f["expense"] == 300 and all(t["user_name"] == "Яна" for t in f["transactions"]), "траты личные: Яна видит только свои", f["expense"])
    dsh = await web_life.dashboard(user=yana)
    check(dsh["month"] == 300 and dsh["personal"]["finance"], "главная: свои траты", dsh["month"])
    txt = await finance.month_summary(yana, 2026, 11)
    check("Мои финансы" in txt and "300" in txt and "Кто вносил" not in txt, "/month в личном режиме", txt)
    h = await undo.history(1)
    check(all(o["user_id"] == 2 for o in h if o["type"] == "finance"), "история: чужие траты скрыты", [o["title"] for o in h])
    tx_alex = sql("select id from transactions where user_id=1 limit 1")
    op, why = await undo.cancel(yana, f"t.{tx_alex}")
    check(op is None, "Яна не может отменить трату Александра", why)
    rows = await ro(2, "SELECT sum(amount) AS s FROM ro.transactions")
    check(float(rows[0][0]) == 300, "вопросы к данным: только свои траты", rows)
    rows = await ro(1, "SELECT sum(amount) AS s FROM ro.transactions")
    check(float(rows[0][0]) == 1000, "…и у Александра свои", rows)
    sql("INSERT INTO budgets (family_id, category_id, monthly_limit) SELECT 1, id, 1000 FROM categories WHERE name='Продукты'")
    b = await budgets.status_rows(1)
    check(b and float(b[0]["spent"]) == 0, "лимиты: у Яны своих продуктов 0", b)

    print("   покупки")
    await shopping.add(alex, [{"title": "молоко"}])
    await as_(yana); await shopping.add(yana, [{"title": "хлеб"}])
    check([x["title"] for x in await shopping.open_items(1)] == ["хлеб"], "личный список покупок у Яны", await shopping.open_items(1))

    print("   питание (теперь общее)")
    await as_(alex)
    sql("INSERT INTO meals (user_id, title, kcal, protein, fat, carbs, eaten_at) VALUES (2, 'Салат', 200, 5, 5, 20, now())")
    n = await web.nutrition_data(days=7, user_id=2, user=alex)
    check(n is not None, "питание общее: можно смотреть Яну")
    await web_life.sections_save({"nutrition": "personal"}, user=alex)
    try:
        await web.nutrition_data(days=7, user_id=2, user=alex); check(False, "личное питание: чужое нельзя")
    except HTTPException as e:
        check(e.status_code == 403, "личное питание: чужое → 403")

    print("   заметки")
    await notes.save(alex, None, "размер обуви у младшей 31")
    await as_(yana)
    r = await notes.list_reply(yana)
    check("31" not in r.text, "заметки личные: Яна не видит заметку Александра", r.text)
    await as_(alex)
    await web_life.sections_save({"notes": "shared"}, user=alex)
    await as_(yana)
    r = await notes.list_reply(yana)
    check("31" in r.text and "Заметки семьи" in r.text, "заметки общие: видны всем", r.text)

    print("   напоминания в личном разделе")
    await as_(alex)
    await web_life.sections_save({"bills": "personal"}, user=alex)
    scope.leave()
    sql("INSERT INTO bills (family_id, user_id, title, amount, due_day, next_due, remind_days) VALUES (1, 2, 'Фитнес', 3000, 5, '2026-11-05', 3)")
    SENT.clear(); await bills.send_due()
    check(any(uid == 2 and "Фитнес" in t for uid, t, _ in SENT) and not any(uid == 1 for uid, _, _ in SENT),
          "личный раздел: напоминание автору (Яне), а не владельцу", SENT)

    # ---------------- Настройки ----------------
    print("3. Настройки")
    await as_(alex)
    s = await web_life.settings_get(user=alex)
    check(s["owner"] and s["me"]["theme"] == "system" and s["me"]["morning"]["time"] == "07:30" and s["me"]["goals"]["kcal"] == 2000,
          "GET /settings: значения по умолчанию", s["me"])
    s2 = await web_life.settings_get(user=await as_(yana))
    check(not s2["owner"], "Яна — не владелец")
    await as_(alex)
    me = await web_life.settings_save({"theme": "dark", "animations": False, "hidden_tabs": ["garage", "home", "bad"],
                                       "notify": {"bills": False, "zzz": False}, "quiet": {"on": True, "from": "22:30", "to": "7:00"},
                                       "mirror": False, "morning": {"on": True, "time": "06:45"},
                                       "digest": {"on": True, "day": 3, "hour": 19},
                                       "goals": {"kcal": 1900, "water": 2500, "steps": "", "weight": 78}}, user=alex)
    check(me["theme"] == "dark" and me["animations"] is False and me["hidden_tabs"] == ["garage"], "оформление и вкладки", me)
    check(me["notify"]["bills"] is False and "zzz" not in me["notify"], "уведомления по темам", me["notify"])
    check(me["quiet"] == {"on": True, "from": "22:30", "to": "07:00"}, "тихие часы", me["quiet"])
    check(me["morning"] == {"on": True, "time": "06:45"} and me["digest"]["day"] == 3 and me["digest"]["hour"] == 19,
          "сводка и разбор", (me["morning"], me["digest"]))
    check(me["goals"]["kcal"] == 1900 and me["goals"]["water"] == 2500 and me["goals"]["weight"] == 78 and "steps" not in me["goals"],
          "цели", me["goals"])
    ui = await web.me(user=alex)
    check(ui["ui"]["theme"] == "dark" and ui["owner"] and ui["modes"]["finance"] == "personal", "/me отдаёт тему и режимы", ui)

    print("   уведомления и тихие часы")
    SENT.clear()
    ok = await notify.send(1, Reply(text="платёж"), topic="bills")
    check(ok is False and not SENT, "тема выключена — не присылает", SENT)
    set_now(datetime(2026, 11, 3, 23, 0, tzinfo=Z)); SENT.clear()
    await notify.send(1, Reply(text="ТО мотоцикла"), topic="garage")
    check(not SENT and sql("select count(*) from outbox where sent_at is null") == "1", "тихие часы: отложено", SENT)
    await notify.send(1, Reply(text="витамин"), topic="meds", urgent=True)
    check(len(SENT) == 1 and "витамин" in SENT[0][1], "лекарства приходят и ночью", SENT)
    await notify.send(1, Reply(text="копия"), quiet="drop")
    check(sql("select count(*) from outbox where sent_at is null") == "1", "копия действия ночью не копится")
    asyncpg.FAKE_NOW = datetime(2026, 11, 4, 7, 1, tzinfo=Z); set_now(datetime(2026, 11, 4, 7, 1, tzinfo=Z)); SENT.clear()
    await notify.flush_outbox()
    check(len(SENT) == 1 and "ТО мотоцикла" in SENT[0][1], "утром отложенное пришло", SENT)
    check(prefs.quiet_until({"quiet": {"on": True, "from": "22:00", "to": "08:00"}}, datetime(2026, 11, 4, 3, 0, tzinfo=Z))
          == datetime(2026, 11, 4, 8, 0, tzinfo=Z), "тихие часы через полночь")
    check(prefs.quiet_until({"quiet": {"on": True, "from": "13:00", "to": "15:00"}}, datetime(2026, 11, 4, 12, 0, tzinfo=Z)) is None,
          "днём вне окна — не тихо")

    print("   копии в Telegram")
    SENT.clear(); set_now(datetime(2026, 11, 4, 12, 0, tzinfo=Z))
    await web._mirror(alex, "тест")
    check(not SENT, "копии из панели выключены", SENT)
    await web._mirror(yana, "тест")
    check(len(SENT) == 1 and SENT[0][0] == 2, "у Яны копии включены", SENT)

    print("   недельный разбор по своему расписанию")
    sql("UPDATE users SET digest_sent_on=NULL")
    thu = datetime(2026, 11, 5, 19, 5, tzinfo=Z); set_now(thu); SENT.clear()
    await digest.send_due()
    check([u for u, *_ in SENT] == [1], "Александру в четверг 19:00 (Яне — в воскресенье)", [u for u, *_ in SENT])
    sun = datetime(2026, 11, 8, 20, 1, tzinfo=Z); set_now(sun); SENT.clear()
    await digest.send_due()
    check([u for u, *_ in SENT] == [2], "Яне в воскресенье", [u for u, *_ in SENT])

    print("   бот")
    set_now(datetime(2026, 11, 4, 12, 0, tzinfo=Z))
    r = await say(alex, "/settings")
    check("Тихие часы: 22:30–07:00" in r.text and "Личные" in r.text and "Не присылать" in r.text, "/settings", r.text)
    r = await press(111, "w:0:login")
    check("web/login" in r.text or "PUBLIC_URL" in r.text, "кнопка «Открыть панель»", r.text)
    scope.leave()
    print("ИТОГ:", "всё прошло" if not FAILS else FAILS)


asyncio.run(main())
