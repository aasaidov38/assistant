"""Проверка отмены записей на настоящей PostgreSQL (через тестовую замену asyncpg)."""
import _env  # noqa: F401  (пути и база)
import asyncio, os, subprocess, sys
os.environ.update(DATABASE_URL="x", CORE_API_KEY="x")

import asyncpg  # заглушка
from app import db, undo, users, brain

from _env import PSQL
sql = lambda s: subprocess.run(PSQL, input=s, capture_output=True, text=True, check=True).stdout.strip()
FAILS = []


def check(cond, name, info=""):
    print(("  ✔ " if cond else "  ✖ ") + name + ("" if cond else f"  → {info}"))
    if not cond:
        FAILS.append(name)


SEED = """
TRUNCATE families, users, user_identities, transactions, transaction_items, meals, metrics, tasks, shopping_items, events RESTART IDENTITY CASCADE;
INSERT INTO families (name) VALUES ('Саидовы'), ('Чужая семья');
INSERT INTO users (family_id, name) VALUES (1,'Александр'), (1,'Яна'), (2,'Чужой');
INSERT INTO user_identities (channel, external_id, user_id) VALUES ('telegram','111',1),('telegram','222',2),('telegram','333',3);
-- деньги
INSERT INTO transactions (family_id,user_id,kind,amount,category_id,merchant,occurred_on,source,created_at)
  VALUES (1,1,'expense',1450,(SELECT id FROM categories WHERE name='Продукты'),'Пятёрочка',current_date,'text',now()-interval '50 min');
INSERT INTO transactions (family_id,user_id,kind,amount,category_id,occurred_on,source,created_at)
  VALUES (1,1,'income',120000,(SELECT id FROM categories WHERE kind='income' ORDER BY sort LIMIT 1),current_date,'text',now()-interval '45 min');
INSERT INTO transactions (family_id,user_id,kind,amount,category_id,merchant,occurred_on,source,group_id,created_at)
  SELECT 1,1,'expense',a,(SELECT id FROM categories WHERE name='Продукты'),'Лента',current_date,'receipt','11111111-2222-3333-4444-555555555555',now()-interval '40 min'
  FROM unnest(ARRAY[100,200,300]) a;
-- еда, привычки
INSERT INTO meals (user_id,title,kcal,created_at) VALUES (1,'Кофе с молоком',120,now()-interval '30 min'),(2,'Салат Яны',300,now()-interval '29 min');
INSERT INTO metrics (user_id,kind,value,created_at) VALUES (1,'water',250,now()-interval '25 min'),(1,'weight',82.5,now()-interval '20 min');
-- дела: своё и пришедшее из TickTick
INSERT INTO tasks (family_id,user_id,title,source,created_at) VALUES (1,1,'Позвонить в банк','text',now()-interval '15 min');
INSERT INTO tasks (family_id,user_id,title,source,created_at) VALUES (1,1,'Из TickTick','ticktick',now()-interval '5 min');
-- покупки
INSERT INTO shopping_items (family_id,title,added_by,created_at) VALUES (1,'Молоко',1,now()-interval '10 min');
-- чужая семья
INSERT INTO meals (user_id,title,kcal) VALUES (3,'Чужой обед',500);
"""


async def main():
    db.pool = asyncpg_conn = __import__("asyncpg").FakeConn()
    sql(SEED)
    alex, yana, other = await users.get(1), await users.get(2), await users.get(3)

    print("1. Распознавание команд без ИИ")
    cases = {
        "отмени": {"mode": "last", "what": "any"},
        "отмени последнюю операцию": {"mode": "last", "what": "any"},
        "отмена": {"mode": "last", "what": "any"},
        "/undo": {"mode": "last", "what": "any"},
        "отмени последнюю трату": {"mode": "last", "what": "finance", "kind": "expense"},
        "удали последний доход": {"mode": "last", "what": "finance", "kind": "income"},
        "удали последнюю еду": {"mode": "last", "what": "meal"},
        "отмени последний прием пищи": {"mode": "last", "what": "meal"},
        "удали последнюю запись воды": {"mode": "last", "what": "metric", "metric": "water"},
        "отмени последний вес": {"mode": "last", "what": "metric", "metric": "weight"},
        "отмени последний чек": {"mode": "last", "what": "finance", "group": True},
        "удали последнюю задачу": {"mode": "last", "what": "task"},
        "убери последнее из списка покупок": {"mode": "last", "what": "shopping"},
        "отмени последнюю покупку": {"mode": "last", "what": "finance", "kind": "expense"},
        "/history": {"mode": "list"},
        "история": {"mode": "list"},
        "последние записи": {"mode": "list"},
        # должны уйти в ИИ
        "отмени лимит на кафе": None, "удали кофе": None, "удали": None, "убери цель по сну": None,
        "отмени встречу": None, "пятерочка 1450 продукты": None, "отмени последнюю трату и еду": None,
    }
    for text, want in cases.items():
        got = undo.parse_command(brain._norm(text))
        check(got == want, f"«{text}»", got)

    print("2. Журнал операций")
    ops = await undo.history(1)
    refs = [o["ref"] for o in ops]
    check(len(ops) == 9, "9 операций семьи (чек одной строкой, задача из TickTick не показывается)", refs)
    check(refs[0] == "s.1", "новые сверху", refs[:3])
    g = next(o for o in ops if o["ref"].startswith("g."))
    check(g["title"] == "🧾 Чек · Лента (3 поз.)" and g["value"] == "−600 ₽", "чек: заголовок и сумма", (g["title"], g["value"]))
    check(not any(o["title"] == "🍽 Чужой обед" for o in ops), "чужая семья не видна")
    check(len(await undo.history(1, user_id=2)) == 1, "фильтр по человеку (Яна: 1 запись)")
    check(len(await undo.history(1, types=["metric"])) == 2, "фильтр по типу (привычки: 2)")

    print("3. «Отмени последнюю операцию» в боте")
    r = await brain.handle_text(alex, "Отмени последнюю операцию", source="text")
    check("Отменил" in r.text and "Молоко" in r.text, "отменена самая свежая запись (покупка «Молоко»)", r.text)
    check(r.buttons and r.buttons[0][0].action == "u:s.1:back", "есть кнопка «Вернуть»", r.buttons)
    r = await undo.handle_action(alex, ["u", "s.1", "back"])
    check("Вернул" in r.text, "кнопка «Вернуть» возвращает", r.text)
    check(sql("SELECT deleted_at IS NULL FROM shopping_items WHERE id=1") == "t", "в базе снова действует")

    print("4. Отмена по типу и по словам")
    r = await brain.handle_text(alex, "отмени последнюю трату", source="text")
    check("Чек · Лента" in r.text, "последняя трата — чек целиком", r.text)
    check(sql("SELECT count(*) FROM transactions WHERE group_id IS NOT NULL AND deleted_at IS NOT NULL") == "3", "все 3 позиции чека отменены")
    r = await undo.handle_action(alex, ["u", "g.11111111-2222-3333-4444-555555555555", "back"])
    check(sql("SELECT count(*) FROM transactions WHERE group_id IS NOT NULL AND deleted_at IS NULL") == "3", "чек вернулся целиком", r.text)
    r = await undo.undo_last(alex, what="meal", query="кофе")
    check("Кофе с молоком" in r.text, "«удали кофе» находит «Кофе с молоком»", r.text)
    r = await undo.undo_last(alex, what="meal", query="кофе")
    check("Не нашёл" in r.text, "повторно — уже нечего отменять", r.text)
    r = await brain.handle_text(alex, "отмени последний вес", source="text")
    check("Вес" in r.text and "82,5 кг" in r.text, "вес отменён, число по-русски", r.text)
    r = await undo.undo_last(alex, what="finance", kind="income")
    check("120 000 ₽" in r.text and "+" in r.text, "отмена последнего дохода", r.text)

    print("5. Строка чека отдельно и потом весь чек")
    line_id = int(sql("SELECT min(id) FROM transactions WHERE group_id IS NOT NULL"))
    op, _ = await undo.cancel(alex, f"t.{line_id}", via="web")
    check(op and op["cancelled"] and op["value"] == "−100 ₽", "одна строка чека отменена (из таблицы панели)", op)
    g = await undo.get(1, "g.11111111-2222-3333-4444-555555555555")
    check(g["title"].endswith("(2 поз.)") and g["value"] == "−500 ₽", "чек показывает 2 оставшиеся позиции", (g["title"], g["value"]))
    await undo.cancel(alex, g["ref"])
    await undo.restore(alex, g["ref"])
    check(sql(f"SELECT deleted_at IS NOT NULL FROM transactions WHERE id={line_id}") == "t",
          "возврат чека не воскрешает строку, отменённую раньше отдельно")

    print("6. Дела")
    r = await undo.undo_last(alex, what="task")
    check("Позвонить в банк" in r.text and not any(b.action.endswith(":back") for row in r.buttons for b in row),
          "дело удалено, кнопки «Вернуть» нет", r.text)
    check(sql("SELECT status FROM tasks WHERE id=1") == "deleted", "статус deleted")
    op, note = await undo.restore(alex, "k.1")
    check(op is None and "TickTick" in note, "вернуть дело нельзя, понятное объяснение", note)
    check(sql("SELECT status FROM tasks WHERE id=2") == "open", "дело из TickTick «последней операцией» не считается")

    print("7. Права")
    op, note = await undo.cancel(other, "m.2")
    check(op is None, "чужая семья не может отменить запись", note)
    op, note = await undo.cancel(yana, "h.1")
    check(op and op["cancelled"], "член семьи может отменить запись другого (как в панели)", note)
    await undo.restore(yana, "h.1")
    check(not undo.valid_ref("t.1;drop") and not undo.valid_ref("x.1") and undo.valid_ref("g.11111111-2222-3333-4444-555555555555"),
          "проверка формата ссылки")

    print("8. Список с кнопками (/history)")
    r = await brain.handle_text(alex, "/history", source="text")
    check("Ваши последние записи" in r.text and "<s>" in r.text, "список с зачёркнутыми отменёнными", r.text[:300])
    acts = [b.action for row in r.buttons for b in row]
    check(any(a.endswith(":del:l") for a in acts) and any(a.endswith(":back:l") for a in acts), "есть и ❌, и ↩️", acts)
    check(all(len(a.encode()) <= 64 for a in acts), "кнопки укладываются в 64 байта Telegram")
    first_del = next(a for a in acts if a.endswith(":del:l"))
    r = await brain.handle_action(__import__("app.schemas", fromlist=["Action"]).Action(external_user_id="111", action=first_del))
    check(r.text.startswith("🗑 Отменил") and "Ваши последние записи" in r.text, "нажатие ❌ в списке обновляет список", r.text[:200])

    print("9. События для n8n")
    ev = sql("SELECT string_agg(DISTINCT type, ',' ORDER BY type) FROM events")
    check("restored" in ev and "deleted" in ev, "пишутся события deleted/restored", ev)

    print("\nИТОГ:", "всё прошло" if not FAILS else f"ошибок: {len(FAILS)} → {FAILS}")


asyncio.run(main())
