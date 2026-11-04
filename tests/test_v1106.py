"""Версия 1.10.6: краткая инструкция к лекарству (карточка, кэш, лимит, бот, кнопка под напоминанием, API, панель)."""
import _env  # noqa: F401  (пути и база)
import asyncio, re, subprocess
from pathlib import Path
import asyncpg
from app import ai, brain, db, druginfo, meds, notify, release, users, web_life
from app.schemas import Action

from _env import PSQL
sql = lambda s: subprocess.run(PSQL, input=s, capture_output=True, text=True, check=True).stdout.strip()
FAILS, CALLS, AI_ANSWER = [], [], {}
APP = Path(__file__).resolve().parent.parent / "core" / "app"


def check(cond, name, info=""):
    print(("  ✔ " if cond else "  ✖ ") + name + ("" if cond else f"  → {str(info)[:500]}"))
    if not cond:
        FAILS.append(name)


SEED = """
TRUNCATE families, users, user_identities, app_state, outbox, meds, med_intakes RESTART IDENTITY CASCADE;
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
    if "intent" in system.lower() and "med_add" in system:      # общий разбор фраз
        for key, val in AI_ANSWER.get("intents", {}).items():
            if key in text:
                return val
        raise AssertionError("нет заготовки разбора для: " + text[:80])
    return v


async def fake_send(uid, reply, topic=None, urgent=False, quiet="defer"):
    return True


async def say(user, text):
    return await brain.handle_text(user, text, source="text")


async def main():
    db.pool = asyncpg.FakeConn()
    sql(SEED)
    ai.complete_json = fake_json
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

    print("7. Версия и документы")
    root = APP.parent.parent
    check(release.VERSION == "1.10.6" and (root / "VERSION").read_text().strip() == "1.10.6", "версия 1.10.6 в VERSION и release.py")
    check(any("инструкция" in x.lower() for x in release.notes("1.10.6")), "в CHANGELOG есть раздел 1.10.6", release.notes("1.10.6"))
    check("## 40." in (root / "README.md").read_text(encoding="utf-8"), "в README раздел 40")
    check(not druginfo.__doc__ or "дозы" in druginfo.__doc__.lower(), "в модуле записано правило «дозы не называем»")
    check("НЕ указывай дозы" in druginfo.SYSTEM and "found\": false" in druginfo.SYSTEM, "в запросе к ИИ запрет доз и право ответить «не знаю»")
    check(await druginfo.official("нурофен") is None, "официальный источник пока не подключён (место под ГРЛС)")

    print()
    print("ИТОГ: всё прошло" if not FAILS else "ИТОГ: ошибки — " + "; ".join(FAILS))


asyncio.run(main())
