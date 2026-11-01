"""Мозг ассистента: понимает сообщение и передаёт его нужному модулю.

Модули: finance (деньги), budgets (лимиты), nutrition (еда), recipes (рецепты), shopping (покупки),
tasks (дела), analytics (вопросы к данным), digest (недельный разбор). Новый модуль подключается так:
  1) свой файл app/<модуль>.py с функциями save / render_saved / handle_action;
  2) новый intent в prompts.TEXT_PARSER;
  3) ветка в handle_text и префикс действия в ACTIONS.
"""
import base64
import logging
import re

from . import (ai, alice, analytics, bank, bills, budgets, debts, digest, finance, garage, habits, meds, meters, morning,
               notes, nutrition, papers, prefs, recipes, savings, scope, shopping, tasks, ticktick, trips, undo, users, web)
from .config import get_settings
from .prompts import PHOTO_PARSER, TEXT_PARSER
from .schemas import Action, Button, Inbound, Reply
from .util import WEEKDAYS, esc, now, plural, today

log = logging.getLogger("brain")

HELP = """🤖 <b>Личный ассистент</b>

Пишите обычным текстом, голосом или присылайте фото.

💰 <b>Деньги</b>
<code>Пятёрочка 1450 продукты</code> · <code>зарплата 120000</code>
📸 фото чека: разложу по категориям и сохраню каждую позицию
<code>лимит на кафе 10000</code>: месячный лимит, предупрежу на 80% и 100%
/budget или «🎯 Бюджет»: сколько осталось по лимитам

🔎 <b>Вопросы к своим данным</b>
<code>сколько потратили на продукты в сентябре?</code>
<code>сравни траты с прошлым месяцем</code>
<code>сколько белка я ел в среднем за неделю?</code>
<code>сколько уходит на сладости по чекам?</code>

🍽 <b>Питание и КБЖУ</b>
<code>съел тарелку борща и два куска хлеба</code>
📸 фото еды (в подписи можно уточнить: «200 г»)
<code>что приготовить на ужин из курицы, чтобы уложиться в 600 ккал</code>
📸 фото холодильника или продуктов: предложу рецепты
/goal 1800: дневная цель по калориям

🛒 <b>Покупки</b>
<code>закончилось молоко</code> · <code>купить хлеб и яйца</code>
/shop или «🛒 Покупки»: общий список семьи, кнопка «📲 В TickTick»

📝 <b>Дела</b>
<code>завтра в 10 позвонить в банк</code>
<code>Яне в субботу забрать посылку, а мне в пятницу оплатить садик</code>
/tasks или «✅ Задачи» · /ticktick: подключить TickTick и настройки (<code>/ticktick off</code> — не сообщать о новых задачах оттуда)

📊 <b>Отчёты</b>
/today: питание и траты за день · /month: финансы за месяц
/week: разбор недели с советами (приходит сам по воскресеньям; /digest off — выключить)
/morning: утренняя сводка (приходит сама в 7:30; <code>/morning 7:00</code> — своё время, <code>/morning off</code>)

📈 <b>Привычки</b>
<code>выпил 2 стакана воды</code> · <code>вес 82.5</code> · <code>9000 шагов</code> · <code>спал 7 часов</code>
<code>цель вода 2 литра</code> · /habits: прогресс за день и неделю

💳 <b>Платежи и накопления</b>
<code>кредит Сбер 15000 каждое 10 число</code> · <code>оплатил ЖКХ 6230</code> · /bills
<code>копим на отпуск 150000 к июлю</code> · <code>отложил на отпуск 10000</code> · /savings
🏦 Пришлите файл выписки (CSV или PDF Сбера/Т-Банка) — сверю с записями и добавлю недостающее

🏍 <b>Гараж</b>
<code>добавь мотоцикл Honda CB400, пробег 21000</code> · <code>пробег мото 23500</code>
<code>менял масло в мото на 23500, 4500 р</code> · <code>заправил мото 1200</code> · /garage

🏠 <b>Дом</b>
<code>ОСАГО на машину до 15.05.2027</code> · <code>паспорт Яны до 2031-05-01</code> · фото гарантийного талона · /docs
<code>счётчик холодная вода кухня</code> · <code>холодная вода 123,456</code> или фото счётчика · /meters
<code>запомни: размер обуви у младшей 31</code> · <code>какой размер обуви у младшей?</code> · /notes

🧳 <b>Поездки</b>
<code>поездка на Байкал с 5 по 12 июля на мото, бюджет 60000, маршрут Иркутск — Листвянка — Ольхон</code>
Траты в эти даты считаются сами · <code>в сборы: дождевик</code> · /trip: бюджет, сборы, погода по маршруту

💊 <b>Лекарства</b>
<code>витамин D по 1 таблетке в 9:00, в упаковке 60</code> · <code>принял витамин D</code> · /meds
Дозы и схему пишите сами, как назначил врач: я только напоминаю и считаю остаток.

🤝 <b>Долги</b>
<code>дал Пете в долг 5000 до 15 ноября</code> · <code>занял у Ивана 3000</code> · <code>Петя вернул 2000</code> · /debts

⚙️ /settings: тема, вкладки, уведомления, тихие часы, личные и общие разделы (в веб-панели)
🌐 /web: вход в веб-панель с графиками (ссылка от бота)
🎙 /alice: привязать Яндекс Алису

↩️ <b>Отмена</b>
<code>отмени последнюю операцию</code> · <code>отмени последнюю трату</code> · <code>удали последнюю еду</code>
<code>удали кофе, я его не пил</code> · /history: ваши последние записи с кнопками ❌ отменить и ↩️ вернуть
Отменённое можно вернуть. Под каждой записью тоже есть кнопки, чтобы исправить или отменить её."""

DENIED = ("⛔ Доступ закрыт.\nВаш ID: <code>{id}</code>\n"
          "Попросите владельца добавить его в ALLOWED_USERS и перезапустить ассистента.")


async def handle_inbound(msg: Inbound) -> Reply:
    user = await users.resolve(msg.channel, msg.external_user_id)
    if not user:
        return Reply(text=DENIED.format(id=esc(msg.external_user_id)))
    await scope.enter(user)
    try:
        if msg.kind == "photo" and msg.file_b64:
            return await handle_photo(user, msg.file_b64, msg.mime or "image/jpeg", msg.text)
        if msg.kind == "document" and msg.file_b64:
            data = base64.b64decode(msg.file_b64)
            _imp, reply = await bank.import_file(user, data, msg.filename or "", msg.mime or "")
            return reply
        if msg.kind == "voice" and msg.file_b64:
            text = await ai.transcribe(base64.b64decode(msg.file_b64), mime=msg.mime or "audio/ogg")
            if not text:
                return Reply(text="🎙 Не расслышал. Попробуйте ещё раз.")
            return await handle_text(user, text, source="voice", header=f"🎙 <i>«{esc(text)}»</i>")
        return await handle_text(user, msg.text, source="text")
    except ai.AIError as e:
        log.warning("AI: %s", e)
        return Reply(text=f"⚠️ Не получилось обратиться к ИИ: {esc(str(e))[:300]}")


async def handle_action(act: Action) -> Reply:
    user = await users.resolve(act.channel, act.external_user_id)
    if not user:
        return Reply(text=DENIED.format(id=esc(act.external_user_id)))
    await scope.enter(user)
    parts = act.action.split(":")
    handler = {"t": finance.handle_action, "g": finance.handle_action,
               "m": nutrition.handle_action, "k": tasks.handle_action,
               "s": shopping.handle_action, "r": recipes.handle_action,
               "h": habits.handle_action, "w": web.handle_action, "u": undo.handle_action,
               "b": bills.handle_action, "i": bank.handle_action, "v": savings.handle_action,
               "o": garage.handle_action, "d": papers.handle_action, "r": meters.handle_action,
               "x": meds.handle_action, "j": trips.handle_action, "e": debts.handle_action}.get(parts[0])
    if not handler or len(parts) < 3:
        return Reply(text="Кнопка устарела.")
    reply = await handler(user, parts)
    # Эти кнопки отвечают отдельным сообщением, чтобы не затирать рецепт или чек
    if parts[0] == "r" or parts[2] in ("items", "tt"):
        reply.new_message = True
    return reply


# ---------- Текст ----------

def _norm(t: str) -> str:
    t = re.sub(r"^[^\w/]+", "", t.strip().lower().replace("ё", "е"))   # убираем эмодзи с кнопок
    return re.sub(r"@\w+", "", t).strip()


async def handle_text(user: users.User, text: str, source: str, header: str = "") -> Reply:
    await scope.enter(user)
    text = (text or "").strip()
    if not text:
        return Reply(text=HELP)
    cmd = _norm(text)

    if cmd in ("/start", "/help", "помощь", "справка", "меню", "help"):
        return Reply(text=HELP)
    if cmd in ("/id",):
        return Reply(text=f"Вы: {esc(user.name)}, внутренний ID {user.id}")
    if cmd in ("/today", "сегодня"):
        return await today_report(user)
    if cmd in ("/month", "месяц"):
        d = today()
        return Reply(text=await finance.month_summary(user, d.year, d.month))
    if cmd in ("/tasks", "задачи", "дела"):
        return await tasks.list_open(user)
    if cmd in ("/shop", "покупки", "список покупок", "список"):
        return await shopping.show(user)
    if cmd in ("/budget", "бюджет", "лимиты"):
        return Reply(text=await budgets.status_text(user))
    if cmd in ("/week", "неделя", "итоги недели", "разбор недели"):
        return await digest.build(user)
    if cmd in ("/habits", "привычки", "здоровье"):
        return await habits.show(user)
    if cmd.startswith("/morning") or cmd in ("сводка", "утро", "утренняя сводка"):
        return await morning.command(user, cmd)
    if cmd.startswith("/web") or cmd in ("панель", "веб", "веб-панель"):
        return await (web.logout_all(user) if "off" in cmd or "выйти" in cmd else web.login_link(user))
    if cmd.startswith("/alice") or cmd in ("алиса",):
        return await alice.link_code(user)
    if cmd in ("/bills", "платежи", "регулярные платежи", "подписки"):
        return await bills.list_reply(user)
    if cmd in ("/savings", "накопления", "копилка", "цели накоплений"):
        return await savings.list_reply(user)
    if cmd in ("/garage", "гараж"):
        return await garage.garage_reply(user)
    if cmd in ("/docs", "документы", "сроки документов"):
        return await papers.list_reply(user)
    if cmd in ("/meters", "счетчики", "показания"):
        return await meters.list_reply(user)
    if cmd.startswith("/notes") or cmd == "заметки":
        return await notes.list_reply(user, cmd[6:].strip() if cmd.startswith("/notes") else "")
    if cmd in ("/trip", "/trips", "поездка", "поездки", "отпуск"):
        return await trips.show(user)
    if cmd in ("/debts", "долги", "кто должен", "кто мне должен"):
        return await debts.list_reply(user)
    if cmd in ("/settings", "настройки"):
        return await settings_reply(user)
    if cmd in ("/meds", "лекарства", "таблетки", "витамины"):
        return await meds.list_reply(user)
    if cmd.startswith("/digest"):
        return await digest.set_enabled(user, "off" not in cmd and "выкл" not in cmd)
    if cmd.startswith("/goal"):
        m = re.search(r"\d{3,5}", cmd)
        if not m:
            return Reply(text="Укажите калории: <code>/goal 1800</code>")
        await nutrition.set_goal(user, int(m.group(0)))
        return Reply(text=f"🎯 Цель: {m.group(0)} ккал в день.")
    if cmd in ("/categories", "категории"):
        cats = await finance.categories()
        return Reply(text="🏷 <b>Категории</b>\n" + "\n".join(
            f"{c['emoji']} {c['name']}" + (" (доход)" if c["kind"] == "income" else "") for c in cats))
    if cmd.startswith("/ticktick"):
        arg = cmd.split(maxsplit=1)[1] if " " in cmd else ""
        if arg in ("off", "выкл", "тихо"):
            return await tasks.set_tt_notify(user, False)
        if arg in ("on", "вкл"):
            return await tasks.set_tt_notify(user, True)
        return await ticktick_connect(user)
    u = undo.parse_command(cmd)
    if u:
        if u["mode"] == "list":
            return await _prefix(header, await undo.list_reply(user))
        u.pop("mode")
        return await _prefix(header, await undo.undo_last(user, **u))
    pasted = ticktick.parse_pasted_url(text)
    if pasted:
        try:
            await ticktick.exchange_code(*pasted)
            return Reply(text="✅ TickTick подключён. Новые задачи будут появляться во «Входящих».")
        except ticktick.TickTickError as e:
            return Reply(text=f"⚠️ {esc(str(e))}")

    # Всё остальное понимает языковая модель
    n = now()
    family = await users.family_members(user.family_id)
    context = await _context(user)
    system = TEXT_PARSER.format(context=context,
        now=n.strftime("%Y-%m-%d %H:%M"), weekday=WEEKDAYS[n.weekday()], tz=get_settings().tz,
        currency=get_settings().currency, user_name=user.name, family=", ".join(m.name for m in family),
        expense_cats=", ".join(await finance.category_names("expense")),
        income_cats=", ".join(await finance.category_names("income")))
    data = await ai.complete_json(system, text)
    intent = data.get("intent")

    if intent == "finance" and data.get("transactions"):
        for t in data["transactions"]:
            if t.get("vehicle"):
                v = await garage.find(user.family_id, t["vehicle"])
                t["vehicle_id"] = v["id"] if v else None
        rows = await finance.save(user, data["transactions"], source=source, raw={"text": text})
        return await _with_budget(user, rows, finance.render_saved(rows, header))
    if intent == "meal" and data.get("meal"):
        meal = await nutrition.save(user, data["meal"], source=source)
        return await nutrition.render_saved(user, meal, header)
    task_list = data.get("tasks") or ([data["task"]] if data.get("task") else [])
    if intent == "task" and task_list:
        if len(task_list) == 1:
            task, note = await tasks.save(user, task_list[0], source=source)
            return tasks.render_saved(task, note, header)
        parts = []
        for t in task_list[:10]:
            task, note = await tasks.save(user, t, source=source)
            parts.append(tasks.render_saved(task, note).text)
        return Reply(text=(header + "\n" if header else "") + "\n\n".join(parts) + "\n\nСписок дел: /tasks")
    if intent == "shopping" and data.get("shopping"):
        return await _prefix(header, await shopping.add(user, data["shopping"]))
    if intent == "budget" and data.get("budget"):
        b = data["budget"]
        return await _prefix(header, await budgets.set_limit(user, b.get("category"), b.get("limit")))
    if intent == "recipe":
        return await recipes.suggest(user, data.get("recipe_request") or text, header=header)
    if intent == "metric" and data.get("metrics"):
        return await _prefix(header, await habits.save(user, data["metrics"], source=source))
    if intent == "metric_goal" and data.get("metric_goal"):
        g = data["metric_goal"]
        return await _prefix(header, await habits.set_goal(user, g.get("kind"), g.get("target")))
    if intent == "data":
        return await analytics.answer(user, text, header)
    life = await _life_intent(user, intent, data, text)
    if life is not None:
        return await _prefix(header, life)
    if intent == "undo":
        u = data.get("undo") or {}
        what = u.get("what") if u.get("what") in undo.TYPES else "any"
        metric = u.get("metric") if u.get("metric") in habits.KINDS else None
        return await _prefix(header, await undo.undo_last(user, what=what, metric=metric,
                                                          query=(u.get("query") or "")[:100] or None))
    answer = data.get("answer") or "Не понял. Напишите /help, чтобы посмотреть примеры."
    return Reply(text=(header + "\n" if header else "") + esc(answer))


async def _context(user: users.User) -> str:
    """Короткий справочник для ИИ: названия техники, платежей, целей, счётчиков, лекарств."""
    parts = []
    vs = await garage.vehicles(user.family_id)
    if vs:
        parts.append("техника: " + ", ".join(f"{v['name']} ({'мото' if v['kind'] == 'moto' else 'машина'})" for v in vs))
    bs = await bills.all_bills(user.family_id)
    if bs:
        parts.append("регулярные платежи: " + ", ".join(b["title"] for b in bs[:20]))
    gs = await savings.goals(user.family_id)
    if gs:
        parts.append("цели накоплений: " + ", ".join(g["title"] for g in gs))
    ms = await meters.meters(user.family_id)
    if ms:
        parts.append("счётчики: " + ", ".join(m["title"] for m in ms))
    tr = await trips.current(user.family_id)
    if tr:
        parts.append(f"поездка: {tr['title']} {tr['start_on']}–{tr['end_on']}")
    ds = await debts.debts(user.family_id)
    if ds:
        parts.append("долги: " + ", ".join(f"{x['person']} ({'нам должны' if x['direction'] == 'lent' else 'мы должны'})"
                                          for x in ds[:15]))
    md = await meds.meds(user.family_id, user.id)
    if md:
        parts.append("лекарства: " + ", ".join(m["title"] for m in md))
    return "; ".join(parts) or "пока ничего"


async def _life_intent(user: users.User, intent: str, data: dict, text: str) -> Reply | None:
    """Платежи, накопления, гараж, документы, счётчики, заметки, лекарства (версия 1.5)."""
    d = data.get(intent) if isinstance(data.get(intent), dict) else {}
    med = data.get("med") or {}
    if intent == "bill":
        return await bills.create(user, d)
    if intent == "bill_paid":
        return await bills.pay_by_name(user, d.get("title"), d.get("amount"))
    if intent == "savings_goal":
        return await savings.create(user, d)
    if intent == "savings_move":
        return await savings.move_by_name(user, d.get("goal"), d.get("amount"))
    if intent == "vehicle":
        return await garage.add_vehicle(user, d)
    if intent == "odometer":
        return await garage.set_mileage(user, d.get("vehicle"), d.get("km"))
    if intent == "service_done":
        return await garage.service_done(user, d)
    if intent == "service_rule":
        return await garage.set_rule(user, d)
    if intent == "paper":
        return await papers.save(user, d)
    if intent == "meter_add":
        return await meters.add(user, d)
    if intent == "meter_reading":
        items = data.get("meter_reading")
        items = items if isinstance(items, list) else [items] if isinstance(items, dict) else []
        return await meters.reading(user, items)
    if intent == "note_save":
        return await notes.save(user, d.get("subject"), d.get("text") or text)
    if intent == "note_ask":
        return await notes.ask(user, d.get("question") or text)
    if intent == "trip":
        return await trips.create(user, d)
    if intent == "trip_items":
        return await trips.add_items(user, data.get("trip_items") or [])
    if intent == "debt":
        return await debts.create(user, d)
    if intent == "debt_repay":
        return await debts.repay(user, d)
    if intent == "med_add":
        return await meds.add(user, d)
    if intent == "med_taken":
        return await meds.take_by_name(user, med.get("name"))
    if intent == "med_stock":
        return await meds.add_stock(user, med.get("name"), med.get("amount"))
    if intent == "med_stop":
        return await meds.stop_by_name(user, med.get("name"))
    return None


# ---------- Фото ----------

async def handle_photo(user: users.User, b64: str, mime: str, caption: str) -> Reply:
    n = now()
    system = PHOTO_PARSER.format(
        now=n.strftime("%Y-%m-%d %H:%M"), tz=get_settings().tz, currency=get_settings().currency,
        expense_cats=", ".join(await finance.category_names("expense")),
        caption=(caption or "").replace("«", "\"").replace("»", "\""))
    data = await ai.complete_json(system, "Разбери фото.", image_b64=b64, mime=mime)
    kind = data.get("type")

    if kind == "receipt":
        items = await finance.receipt_to_items(data)
        rows = await finance.save(user, items, source="receipt", raw=data, group=True)
        head = f"🧾 Чек{': ' + esc(data['merchant']) if data.get('merchant') else ''}"
        reply = await _with_budget(user, rows, finance.render_saved(rows, head))
        crossed = await shopping.cross_off_from_receipt(user, data.get("items") or [])
        if crossed:
            reply.text += "\n\n🛒 Вычеркнул из списка покупок: " + ", ".join(esc(c) for c in crossed)
        return reply
    if kind == "products":
        return await recipes.suggest(user, caption or "что приготовить из этих продуктов",
                                     products=[str(p) for p in data.get("products") or []][:30],
                                     header="📸 Продукты на фото")
    if kind in ("warranty", "document"):
        att = await papers.save_attachment(user.family_id, base64.b64decode(b64), mime)
        doc = {**data, "kind": "warranty" if kind == "warranty" else (data.get("kind") or "document")}
        return await papers.save(user, doc, attachment_id=att)
    if kind == "meter":
        att = await papers.save_attachment(user.family_id, base64.b64decode(b64), mime)
        return await meters.reading(user, [{"meter": caption or None, "kind": data.get("kind"), "value": data.get("value")}],
                                    attachment_id=att)
    if kind == "food":
        meal = await nutrition.save(user, data, source="photo")
        head = "📸 Оценка по фото, примерная" + (" (уверенность низкая)" if data.get("confidence") == "low" else "")
        return await nutrition.render_saved(user, meal, head)
    return Reply(text=f"🤔 Не похоже ни на чек, ни на еду, ни на продукты: {esc(data.get('description') or '')}")


async def _with_budget(user: users.User, rows: list[dict], reply: Reply) -> Reply:
    """Добавляет к ответу предупреждение о лимите, если он почти исчерпан."""
    try:
        warn = await budgets.check_after_save(user, rows)
    except Exception:
        log.exception("Проверка лимитов")
        warn = ""
    if warn:
        reply.text += "\n\n" + warn
    return reply


async def _prefix(header: str, reply: Reply) -> Reply:
    if header:
        reply.text = header + "\n" + reply.text
    return reply


# ---------- Отчёты и подключения ----------

async def today_report(user: users.User) -> Reply:
    d = today()
    spent, n = await finance.today_expenses(user, d)
    text = f"📊 <b>{d:%d.%m.%Y}, {WEEKDAYS[d.weekday()]}</b>\n\n"
    text += await nutrition.day_report(user, d)
    text += f"\n\n💸 {'Мои траты' if scope.is_personal('finance') else 'Траты семьи'} сегодня: <b>{finance.money(spent)}</b> ({plural(n, 'запись', 'записи', 'записей')})"
    return Reply(text=text)


async def settings_reply(user: users.User) -> Reply:
    """/settings: коротко текущие настройки и ссылка в панель, где их можно поменять."""
    s = await prefs.full(user)
    modes = await scope.modes(user.family_id)
    on = [t for k, t in prefs.TOPICS.items() if s["notify"].get(k)]
    off = [t for k, t in prefs.TOPICS.items() if not s["notify"].get(k)]
    q = s["quiet"]
    text = ("⚙️ <b>Настройки</b>\n\n"
            f"☀️ Утренняя сводка: {'в ' + s['morning']['time'] if s['morning']['on'] else 'выключена'}\n"
            f"🗓 Недельный разбор: {WEEKDAYS[s['digest']['day']] + ', ' + str(s['digest']['hour']) + ':00' if s['digest']['on'] else 'выключен'}\n"
            f"🌙 Тихие часы: {q['from'] + '–' + q['to'] if q['on'] else 'выключены'}\n"
            + (f"🔕 Не присылать: {', '.join(off)}\n" if off else "🔔 Все напоминания включены\n")
            + "\n<b>Разделы</b>\n👤 Личные: " + ", ".join(scope.SECTIONS[k][0] for k, v in modes.items() if v == "personal")
            + "\n👨‍👩‍👧 Общие: " + ", ".join(scope.SECTIONS[k][0] for k, v in modes.items() if v == "shared")
            + "\n\nМенять удобно в веб-панели: вкладка «⚙️ Настройки» (/web).")
    return Reply(text=text, buttons=[[Button(text="🌐 Открыть панель", action="w:0:login")]])


async def ticktick_connect(user: users.User) -> Reply:
    s = get_settings()
    if not ticktick.configured():
        return Reply(text="TickTick ещё не настроен: заполните TICKTICK_CLIENT_ID и TICKTICK_CLIENT_SECRET в .env "
                          "(инструкция в README, раздел «TickTick»).")
    link = ticktick.auth_link(user.id)
    info = await ticktick.token_info(user.id)
    status = ""
    if info["connected"]:
        import time as _t
        last = f"{int((_t.time() - info['last_sync']) // 60)} мин назад" if info.get("last_sync") else "ещё не было"
        days = int((info["expires_at"] - _t.time()) // 86400)
        status = (f"✅ TickTick подключён. Синхронизация: {last}"
                  + (f"\n⚠️ Последняя ошибка: {esc(info['last_error'])}" if info.get("last_error") else "")
                  + "\n" + ("🔔 Сообщаю о новых задачах из TickTick (<code>/ticktick off</code> — не сообщать)"
                            if info["notify"] else "🔕 О новых задачах из TickTick не сообщаю (<code>/ticktick on</code> — включить)")
                  + f"\nДоступ действует ещё {days} дн. Можно переподключить по ссылке.\n\n")
    elif info.get("expires_at"):
        status = "⚠️ Доступ к TickTick истёк, подключите заново.\n\n"
    if s.public_url:
        how = "Откройте ссылку и нажмите «Allow». После этого я пришлю подтверждение."
    else:
        how = ("Откройте ссылку и нажмите «Allow». Браузер откроет страницу, которая не загрузится, "
               "это нормально. Скопируйте адрес этой страницы целиком и пришлите мне.")
    buttons = []
    if info["connected"]:
        buttons = [[Button(text="🔕 Не присылать новые из TickTick", action="k:0:ttn:0") if info["notify"]
                    else Button(text="🔔 Присылать новые из TickTick", action="k:0:ttn:1")],
                   [Button(text="🔄 Синхронизировать сейчас", action="k:0:sync")]]
    return Reply(text=f"{status}🔗 <a href=\"{esc(link)}\">Подключить TickTick</a>\n\n{how}", buttons=buttons)
