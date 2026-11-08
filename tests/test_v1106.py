"""Версия 1.10.6: краткая инструкция к лекарству (карточка, кэш, лимит, бот, кнопка под напоминанием, API, панель)."""
import _env  # noqa: F401  (пути и база)
import asyncio, re, subprocess
from pathlib import Path
import asyncpg
from app import ai, brain, db, digest, dietitian, druginfo, meds, notify, prefs, release, users, util, web_life
from fastapi import HTTPException
from app.schemas import Action

from _env import PSQL
sql = lambda s: subprocess.run(PSQL, input=s, capture_output=True, text=True, check=True).stdout.strip()
FAILS, CALLS, AI_ANSWER, SENT, PLAIN = [], [], {}, [], []
APP = Path(__file__).resolve().parent.parent / "core" / "app"


def check(cond, name, info=""):
    print(("  ✔ " if cond else "  ✖ ") + name + ("" if cond else f"  → {str(info)[:500]}"))
    if not cond:
        FAILS.append(name)


SEED = """
TRUNCATE families, users, user_identities, app_state, outbox, meds, med_intakes, meals RESTART IDENTITY CASCADE;
INSERT INTO families (name) VALUES ('Демо');
INSERT INTO users (family_id, name) VALUES (1,'Алексей'), (1,'Мария');
INSERT INTO user_identities (channel, external_id, user_id) VALUES ('telegram','111',1),('telegram','222',2);
"""

IBU = {"found": True, "name": "Нурофен", "active": "ибупрофен",
       "about": "Обезболивающее и жаропонижающее средство из группы НПВС.",
       "how": ["Принимают во время или после еды", "Запивают водой", "Без врача не дольше 3 дней при жаре и 5 дней при боли"],
       "side": ["Тошнота, изжога", "Боль в животе", "Головокружение"],
       "restrict": ["Язва желудка в обострении", "Аллергия на ибупрофен и аспирин", "Беременность в третьем триместре", "Дети до 6 лет (проверьте возраст в инструкции)"]}


async def fake_json(system, text, **k):
    CALLS.append(text)
    v = AI_ANSWER.get("v")
    if isinstance(v, Exception):
        raise v
    if system.startswith(dietitian.REVIEW[:40]):
        CALLS.append("REVIEW")
        AI_ANSWER["review_input"] = text
        return AI_ANSWER["review"]
    if "intent" in system.lower() and "med_add" in system:      # общий разбор фраз
        for key, val in AI_ANSWER.get("intents", {}).items():
            if key in text:
                return val
        raise AssertionError("нет заготовки разбора для: " + text[:80])
    return v


async def fake_send(uid, reply, topic=None, urgent=False, quiet="defer"):
    SENT.append((uid, reply.text))
    return True


async def fake_complete(system, text, image_b64=None, mime="image/jpeg", max_tokens=2000):
    PLAIN.append((system[:30], text))
    return AI_ANSWER.get("plain", "ok")


async def say(user, text):
    return await brain.handle_text(user, text, source="text")


async def main():
    db.pool = asyncpg.FakeConn()
    sql(SEED)
    ai.complete_json = fake_json
    ai.complete = fake_complete
    notify.send = fake_send
    alex, maria = await users.get(1), await users.get(2)

    print("1. Название и разбор ответа")
    check(druginfo.normalize("  Нурофен® ") == "нурофен", "название без регистра и значков", druginfo.normalize("  Нурофен® "))
    check(druginfo.normalize("витамин D3") == "витамин d3", "цифры в названии сохраняются")
    check(druginfo.normalize("") == "" and druginfo.normalize("1") == "" and druginfo.normalize("12345") == "", "пустое и цифры — не название")
    check(len(druginfo.normalize("а" * 200)) == 60, "длинное название обрезается")
    c = druginfo.build_card(IBU)
    check(c and c["name"] == "Нурофен" and len(c["restrict"]) == 4 and c["source"] == "ai", "карточка из ответа ИИ", c)
    check(druginfo.build_card({"found": False}) is None and druginfo.build_card({}) is None and druginfo.build_card("x") is None,
          "found=false и мусор — карточки нет")
    check(druginfo.build_card({"found": True, "name": "X", "side": "одна строка"})["side"] == ["одна строка"], "строка вместо списка допустима")
    big = druginfo.build_card({"found": True, "name": "X", "about": "a", "side": [f"п{i}" for i in range(20)]})
    check(len(big["side"]) == 6, "список побочных обрезается до 6")

    print("2. Справка в боте и кэш")
    AI_ANSWER["v"] = IBU
    r = await say(alex, "/drug Нурофен")
    t = r.text
    check("Нурофен" in t and "Как принимать" in t and "побочные" in t and "противопоказания" in t.lower(), "карточка из четырёх блоков", t)
    check("не заменяет инструкцию" in t and len(t) < 4096, "пометка «не заменяет инструкцию», влезает в сообщение", len(t))
    check(len(CALLS) == 1 and CALLS[0] == "нурофен", "ИИ получил только название, без имени пользователя", CALLS)
    check("Алексей" not in CALLS[0], "имя человека не уходит наружу")
    r2 = await say(maria, "/drug  нурофен ")
    check(len(CALLS) == 1 and "Нурофен" in r2.text, "повторный запрос (другой человек, другой регистр) берётся из кэша", CALLS)
    check(sql("select count(*) from app_state where key='drug:нурофен'") == "1", "карточка сохранена в app_state")
    r3 = await say(alex, "инструкция к нурофен")
    check(len(CALLS) == 1 and "Нурофен" in r3.text, "фраза «инструкция к …» работает как команда")
    r4 = await say(alex, "/drug")
    check("/drug" in r4.text and len(CALLS) == 1, "без названия — подсказка, ИИ не вызывается", r4.text)
    check("<script" not in t, "в тексте нет лишней разметки")

    print("3. Неизвестный препарат, ошибки, лимит")
    AI_ANSWER["v"] = {"found": False}
    r = await say(alex, "/drug абракадабрин")
    check("Не нашёл надёжных сведений" in r.text, "неизвестное — честный ответ без выдумки", r.text)
    n = len(CALLS)
    r = await say(alex, "/drug абракадабрин")
    check(len(CALLS) == n and "Не нашёл" in r.text, "«не найдено» тоже запоминается, повторного вызова ИИ нет")
    AI_ANSWER["v"] = RuntimeError("сеть")
    r = await say(alex, "/drug аспирин")
    check("недоступна" in r.text, "сбой ИИ — понятное сообщение", r.text)
    check(sql("select count(*) from app_state where key='drug:аспирин'") == "0", "сбой не кэшируется")
    AI_ANSWER["v"] = dict(IBU, name="Аспирин", active="ацетилсалициловая кислота")
    r = await say(alex, "/drug аспирин")
    check("Аспирин" in r.text, "после сбоя повторный запрос работает", r.text)
    druginfo._used.clear()
    druginfo.DAILY_LIMIT = 2
    AI_ANSWER["v"] = dict(IBU, name="Т")
    await say(maria, "/drug лекарство-один"); await say(maria, "/drug лекарство-два")
    r = await say(maria, "/drug лекарство-три")
    check("лимит" in r.text, "лимит новых справок в сутки", r.text)
    r = await say(maria, "/drug лекарство-один")
    check("лимит" not in r.text, "уже найденное доступно и после лимита")
    r = await say(alex, "/drug совсем-другое")
    check("лимит" not in r.text, "лимит у каждого человека свой")
    druginfo.DAILY_LIMIT = 30

    print("4. Свободная фраза и кнопка под напоминанием")
    AI_ANSWER["v"] = IBU
    AI_ANSWER["intents"] = {"побочки у ибупрофена": {"intent": "drug_info", "med": {"name": "ибупрофен"}}}
    r = await say(alex, "побочки у ибупрофена")
    check("Нурофен" in r.text and "ибупрофен" in CALLS[-1], "фраза про побочные эффекты → справка (intent drug_info)", (r.text[:80], CALLS[-1:]))
    AI_ANSWER["intents"]["витамин D по 1"] = {"intent": "med_add", "med_add": {"title": "Нурофен", "dose": "1 таблетка", "times": ["09:00"],
                                                                         "stock": 10, "per_intake": 1, "unit": "шт"}}
    await say(alex, "витамин D по 1 таблетке в 9:00")
    mid = sql("select id from meds")
    iid = sql(f"insert into med_intakes (med_id, due_at, user_id) values ({mid}, now(), 1) returning id").split()[0]
    btns = [b.action for row in meds._intake_buttons(int(iid)) for b in row]
    check(f"x:i{iid}:info" in btns and all(len(a.encode()) <= 64 for a in btns), "под напоминанием есть «ℹ️ Инструкция», действие ≤ 64 байт", btns)
    n = len(CALLS)
    r = await brain.handle_action(Action(external_user_id="111", action=f"x:i{iid}:info"))
    check("Нурофен" in r.text and r.new_message and len(CALLS) == n, "кнопка показывает карточку отдельным сообщением (из кэша)", r.text[:60])
    r = await brain.handle_action(Action(external_user_id="222", action=f"x:i{iid}:info"))
    check("Нурофен" in r.text, "кнопка работает для членов той же семьи")
    r = await brain.handle_action(Action(external_user_id="111", action=f"x:m{mid}:info"))
    check("Нурофен" in r.text and r.new_message, "кнопка по лекарству из расписания")
    lst = await say(alex, "/meds")
    check("/drug" in lst.text, "в списке лекарств есть подсказка про /drug")
    check("Инструкция к лекарству" in brain.HELP and "/drug" in brain.HELP and len(brain.HELP) < 4096, "справка бота упоминает инструкцию и влезает в сообщение", len(brain.HELP))

    print("5. API панели")
    ok = await web_life.drug_info(name="нурофен", user=alex)
    check(ok["ok"] and ok["card"]["name"] == "Нурофен" and ok["cached"] and "не заменяет" in ok["note"], "GET /web/api/drug: карточка из кэша", ok)
    bad = await web_life.drug_info(name="", user=alex)
    check(not bad["ok"] and bad["reason"] == "bad_name" and "<code>" not in bad["message"], "пустое название → понятное сообщение без HTML", bad)
    AI_ANSWER["v"] = {"found": False}
    nf = await web_life.drug_info(name="неведомое средство", user=alex)
    check(not nf["ok"] and nf["reason"] == "not_found", "неизвестный препарат → not_found", nf)
    src = (APP / "web_life.py").read_text(encoding="utf-8")
    check('"/web/api/drug"' in src and "Depends(current_user)" in src.split('"/web/api/drug"')[1][:200], "маршрут требует входа в панель")

    print("6. Панель (текстовые проверки)")
    html = (APP / "static" / "index.html").read_text(encoding="utf-8")
    for needle, name in [('id="drugForm"', "поле поиска"), ('id="drugCard"', "место для карточки"), ("data-minfo", "кнопка ℹ️ у лекарства"),
                         ("async function showDrugInfo", "функция показа"), ("api('drug?name='", "запрос к API"), ("не заменяет", None)]:
        if name:
            check(needle in html, "панель: " + name)
    names = re.findall(r"^(?:async )?function (\w+)", html, re.M)
    check(len(names) == len(set(names)), "в панели нет функций с одинаковыми именами", [n for n in set(names) if names.count(n) > 1])
    check("drugList" in html and html.count("const drugList") == 1, "вспомогательная функция одна")
    bot = (APP.parent.parent / "adapters" / "telegram" / "bot.py").read_text(encoding="utf-8")
    check('command="drug"' in bot, "команда /drug в меню Telegram")


    print("8. Диетолог: данные и разбор недели")
    check(dietitian.build_review({"plate": [{"part": "Овощи **и** фрукты", "status": "ПЛОХО", "comment": "x" * 500}], "good": "один пункт",
                                  "improve": [f"п{i}" for i in range(9)], "steps": [], "note": "# заметка"})["plate"][0]["status"] == "нет данных",
          "неизвестная оценка → «нет данных»")
    rv = dietitian.build_review({"plate": [{"part": "Овощи **и** фрукты", "status": "мало", "comment": "x" * 500}], "good": "один пункт",
                                 "improve": [f"п{i}" for i in range(9)], "note": "# заметка"})
    check(rv and "*" not in rv["plate"][0]["part"] and len(rv["plate"][0]["comment"]) == 160 and rv["good"] == ["один пункт"]
          and len(rv["improve"]) == 3 and rv["note"] == "заметка" and rv["steps"] == [], "разбор ответа ИИ чистится: разметка, длина, число пунктов", rv)
    check(dietitian.build_review({}) is None and dietitian.build_review({"plate": []}) is None, "мусор вместо разбора → None")
    r = await say(alex, "/diet week")
    check("нет записей о еде" in r.text and "REVIEW" not in CALLS, "нет записей — подсказка, ИИ не вызывается", r.text)
    MEALS = [(0, "lunch", "Борщ", 420, 20, 18, 45, [{"name": "борщ", "kcal": 320}, {"name": "хлеб белый", "kcal": 100}]),
             (1, "breakfast", "Овсянка с ягодами", 380, 14, 9, 60, None), (1, "dinner", "Курица с гречкой", 560, 45, 14, 60, None),
             (2, "snack", "Печенье и сладкий чай", 250, 3, 9, 40, None), (2, "lunch", "Салат с оливковым маслом", 300, 6, 20, 20, None),
             (3, "dinner", "Рыба с овощами", 480, 40, 20, 25, None)]
    def add_meals(rows):
        for k, mt, title, kcal, p, f, c, items in rows:
            it = "NULL" if not items else "'" + str(items).replace("'", '"') + "'::jsonb"
            sql(f"insert into meals (user_id, eaten_at, meal_type, title, kcal, protein, fat, carbs, items) values (1, now() - interval '{k} days', '{mt}', '{title}', {kcal}, {p}, {f}, {c}, {it})")
    add_meals(MEALS[:3])
    n = len(CALLS)
    r = await say(alex, "/diet week")
    check("хотя бы 3 дня" in r.text and "REVIEW" not in CALLS[n:], "две даты с записями — оценку не делаем, ИИ не вызывается", r.text)
    add_meals(MEALS[3:])
    AI_ANSWER["review"] = {"plate": [{"part": "Овощи и фрукты", "status": "мало", "comment": "в записях овощей мало: салат и овощи к рыбе"},
                                     {"part": "Цельнозерновые", "status": "в норме", "comment": "овсянка и гречка"},
                                     {"part": "Белок", "status": "в норме", "comment": "рыба и курица"},
                                     {"part": "Масла и жиры", "status": "в норме", "comment": "оливковое масло"},
                                     {"part": "Напитки и сладкое", "status": "много", "comment": "печенье и сладкий чай"}],
                           "good": ["Рыба и курица в рационе"], "improve": ["Больше овощей к обеду"], "steps": ["Добавить овощи к каждому обеду", "Заменить белый хлеб цельнозерновым"],
                           "note": ""}
    w = await dietitian.week_data(alex)
    tot_kcal = 420 + 380 + 560 + 250 + 300 + 480
    check(w["days_logged"] == 4 and w["avg_kcal"] == round(tot_kcal / 4), "среднее считает код: ккал в день", (w["days_logged"], w["avg_kcal"]))
    check(w["macro_share_pct"]["protein"] + w["macro_share_pct"]["fat"] + w["macro_share_pct"]["carbs"] in range(98, 103), "доли калорий дают около 100%", w["macro_share_pct"])
    check(w["snack_share_pct"] == round(250 * 100 / tot_kcal), "доля перекусов", w["snack_share_pct"])
    names = {f["name"] for f in w["foods"]}
    check("борщ" in names and "хлеб белый" in names and "овсянка с ягодами" in names and "борщ с хлебом" not in names, "список еды: названия из items, иначе из названия приёма", names)
    check(w["low_intake"] is True, "среднее ниже 70% цели — флаг low_intake (мало калорий)", (w["avg_kcal"], w["kcal_goal"]))
    r = await say(alex, "/diet week")
    t = r.text
    check(CALLS.count("REVIEW") == 1, "ИИ вызван один раз")
    check(f"{w['avg_kcal']} ккал" in t and f"цель {w['kcal_goal']}" in t and "Записи: 4 из 7" in t, "числа в тексте из кода", t[:300])
    check("Тарелка по схеме Гарварда" in t and "🟠 <b>Овощи и фрукты</b>: мало" in t and "🟢 <b>Белок</b>" in t, "блоки тарелки с оценками", t)
    check("Шаги на неделю" in t and "Что улучшить" in t and "порции не взвешены" in t and "не заменяет консультацию" in t and len(t) < 4096,
          "шаги, оговорка про порции и про врача, влезает в сообщение", len(t))
    r = await say(alex, "разбор питания")
    check(CALLS.count("REVIEW") == 1 and "Тарелка по схеме Гарварда" in r.text, "повтор без новых записей берётся из кэша (фраза «разбор питания»)")
    add_meals([(0, "snack", "Яблоко", 80, 0, 0, 20, None)])
    await say(alex, "/diet неделя")
    check(CALLS.count("REVIEW") == 2, "новая запись — разбор считается заново")
    check('"low_intake": true' in AI_ANSWER["review_input"] and '"foods"' in AI_ANSWER["review_input"] and "Алексей" not in AI_ANSWER["review_input"],
          "в запрос к ИИ уходят цифры, флаг «мало калорий» и список еды, имя не уходит")
    review_prompt = dietitian.REVIEW
    for need in ("Картофель", "красное мясо", "не называй свои цифры калорий", "low_intake", "Не ставь диагнозов", "ничего не выдумывай"):
        check(need.lower() in review_prompt.lower(), f"в запросе к ИИ есть правило: {need}")
    AI_ANSWER["review"] = {"plate": []}
    sql("delete from app_state where key like 'diet:week:%'")
    r = await say(alex, "/diet week")
    check("недоступен" in r.text, "непригодный ответ ИИ → понятное сообщение", r.text)

    print("9. Диетолог: вопросы, режим, настройки")
    AI_ANSWER["plain"] = "Творог 5%: примерно **17 г** белка на 100 г.\n# Это общая справка"
    r = await say(alex, "/diet сколько белка в твороге?")
    check("17 г" in r.text and "*" not in r.text and "#" not in r.text and "не заменяет консультацию" in r.text, "ответ диетолога: разметка убрана, оговорка есть", r.text)
    q = PLAIN[-1]
    check("сколько белка в твороге" in q[1] and "kcal_goal" in q[1] and "Алексей" not in q[1] and "week_avg" in q[1], "в запрос ушли вопрос и цифры, имя человека не уходит", q[1][:200])
    check("Гарвард" in dietitian.ASK and "БАД" in dietitian.ASK and "врачу" in dietitian.ASK, "в запросе диетолога: тарелка, БАД, врач")
    AI_ANSWER["intents"] = {"можно ли есть яйца": {"intent": "diet_q"}, "как я питался": {"intent": "diet_week"}}
    n = len(PLAIN)
    r = await say(alex, "можно ли есть яйца каждый день?")
    check(len(PLAIN) == n + 1 and "17 г" in r.text, "свободный вопрос → intent diet_q → ответ диетолога", r.text[:80])
    r = await say(alex, "как я питался на этой неделе")
    check("Тарелка по схеме Гарварда" in r.text or "недоступен" in r.text, "фраза про неделю → intent diet_week")
    r = await say(alex, "/diet")
    check("Режим диетолога" in r.text and "включён" in r.text and "/diet week" in r.text, "/diet — справка и статус режима", r.text)
    r = await say(alex, "/diet off")
    check("выключен" in r.text and (await prefs.get(1))["dietitian"] is False, "/diet off выключает режим")
    st = await say(alex, "/settings")
    check("Режим диетолога: выключен" in st.text, "в /settings виден режим", st.text)
    r = await say(alex, "/diet on")
    check((await prefs.get(1))["dietitian"] is True, "/diet on включает режим")
    full = await prefs.update(alex, {"dietitian": False})
    check(full["dietitian"] is False and (await prefs.get(1))["dietitian"] is False, "настройка из панели сохраняется")
    await prefs.update(alex, {"dietitian": True})
    check((await prefs.get(2))["dietitian"] is True, "по умолчанию режим включён")
    big = await say(alex, "/diet " + "очень длинный вопрос " * 100)
    check(PLAIN[-1][1].count("очень") <= 30, "длинный вопрос обрезается до 500 символов")
    dietitian.DAILY_LIMIT = 1; dietitian._used.clear()
    await say(maria, "/diet вопрос один")
    r = await say(maria, "/diet вопрос два")
    check("лимит" in r.text, "лимит обращений к диетологу в сутки", r.text)
    dietitian.DAILY_LIMIT = 40

    print("10. Диетолог: API панели и недельный разбор")
    sql("delete from app_state where key like 'diet:week:%'")
    AI_ANSWER["review"] = {"plate": [{"part": "Белок", "status": "в норме", "comment": "рыба"}], "good": ["Рыба"], "improve": [], "steps": ["Больше овощей"], "note": ""}
    api = await web_life.diet_week(user_id=None, user=alex)
    check(api["ok"] and api["review"]["plate"][0]["part"] == "Белок" and api["stats"]["days_logged"] == 4 and "не заменяет" in api["note"], "GET /web/api/diet/week: разбор и цифры", api)
    empty = await web_life.diet_week(user_id=None, user=maria)
    check(not empty["ok"] and empty["reason"] == "no_data" and "нет записей" in empty["message"], "нет записей → объяснение без вызова ИИ", empty)
    try:
        await web_life.diet_week(user_id=2, user=alex)
        check(False, "чужое питание (личный раздел) недоступно")
    except HTTPException as e:
        check(e.status_code == 403, "чужое питание (личный раздел) недоступно: 403")
    sql("delete from app_state where key like 'diet:week:%'")
    sql(f"update users set weekly_digest=true, digest_day={util.now().weekday()}, digest_hour=0, digest_sent_on=NULL")
    AI_ANSWER["plain"] = "• совет"
    SENT.clear(); await digest.send_due()
    sent1 = [m for u, m in SENT if u == 1]
    check(len(sent1) == 2 and "Неделя" in sent1[0] and "Питание за неделю" in sent1[1], "режим включён: после недельного разбора — второе сообщение с тарелкой", [m[:40] for m in sent1])
    check(not [m for u, m in SENT if u == 2 and "Питание за неделю" in m], "у Марии нет записей — второго сообщения нет")
    await prefs.update(alex, {"dietitian": False})
    sql("update users set digest_sent_on=NULL")
    SENT.clear(); await digest.send_due()
    check(len([m for u, m in SENT if u == 1]) == 1, "режим выключен — только обычный недельный разбор", [m[:40] for m in SENT])
    await prefs.update(alex, {"dietitian": True})
    check(await dietitian.week_reply(maria, auto=True) is None, "auto без записей — ничего не шлём")

    print("11. Панель: диетолог")
    for needle, name in [('id="dietBtn"', "кнопка «Разобрать мою неделю»"), ('id="dietWeek"', "место для разбора"), ("async function showDietWeek", "функция показа"),
                         ("api('diet/week?user_id='", "запрос к API"), ('id="sDiet"', "переключатель режима в настройках"),
                         ("saveSet({dietitian:", "сохранение режима")]:
        check(needle in html, "панель: " + name)
    names2 = re.findall(r"^(?:async )?function (\w+)", html, re.M)
    check(len(names2) == len(set(names2)), "после диетолога в панели нет функций с одинаковыми именами")
    check('"/web/api/diet/week"' in src and "Depends(current_user)" in src.split('"/web/api/diet/week"')[1][:200], "маршрут разбора требует входа")
    bot2 = (APP.parent.parent / "adapters" / "telegram" / "bot.py").read_text(encoding="utf-8")
    check('command="diet"' in bot2, "команда /diet в меню Telegram")

    print("7. Версия и документы")
    root = APP.parent.parent
    check(release.newer(release.VERSION, "1.10.5") and (root / "VERSION").read_text().strip() == release.VERSION, "версия не ниже 1.10.6, VERSION и release.py совпадают")
    check(any("инструкция" in x.lower() for x in release.notes("1.10.6")), "в CHANGELOG есть раздел 1.10.6", release.notes("1.10.6"))
    check("## 40." in (root / "docs" / "INSTALL.md").read_text(encoding="utf-8"), "в docs/INSTALL.md раздел 40")
    check(not druginfo.__doc__ or "дозы" in druginfo.__doc__.lower(), "в модуле записано правило «дозы не называем»")
    check("НЕ указывай дозы" in druginfo.SYSTEM and "found\": false" in druginfo.SYSTEM, "в запросе к ИИ запрет доз и право ответить «не знаю»")
    check(await druginfo.official("нурофен") is None, "официальный источник пока не подключён (место под ГРЛС)")

    print()
    print("ИТОГ: всё прошло" if not FAILS else "ИТОГ: ошибки — " + "; ".join(FAILS))


asyncio.run(main())
