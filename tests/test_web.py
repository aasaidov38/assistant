import _env  # noqa: F401  (пути и база)
import asyncio, os, sys, subprocess
os.environ.update(DATABASE_URL="x", CORE_API_KEY="x")
import asyncpg
from app import db, users, web, notify
from fastapi import HTTPException
sent = []
async def fake_send(uid, reply, **kw): sent.append((uid, reply.text, [b.action for r in reply.buttons for b in r])); return True
notify.send = fake_send
exec(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_undo.py")).read().split("async def main")[0].split("FAILS = []")[1].split('SEED = """')[0])  # check()
SEED = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_undo.py")).read().split('SEED = """')[1].split('"""')[0]
from _env import PSQL
sql = lambda s: subprocess.run(PSQL, input=s, capture_output=True, text=True, check=True).stdout.strip()
FAILS=[]
async def main():
    db.pool = asyncpg.FakeConn(); sql(SEED)
    alex, yana = await users.get(1), await users.get(2)
    h = await web.history_data(type="all", user_id=None, days=30, cancelled=1, user=alex)
    check(len(h["ops"]) == 9 and isinstance(h["ops"][0]["at"], str) and "search" not in h["ops"][0], "история: 9 записей, даты строками", h["ops"][:1])
    r = await web.op_cancel("m.1", user=alex)
    check(r["ok"] and "Кофе" in r["text"] and r["op"]["can_restore"], "отмена из панели", r)
    check(any("Веб-панель" in t and "u:m.1:back" in a for _, t, a in sent), "копия в Telegram с кнопкой «Вернуть»", sent[-1:])
    try:
        await web.op_cancel("m.1", user=alex); check(False, "повторная отмена → 409")
    except HTTPException as e:
        check(e.status_code == 409, "повторная отмена → 409", e.detail)
    r = await web.op_restore("m.1", user=alex)
    check(r["ok"] and not r["op"]["cancelled"], "возврат из панели", r)
    sent.clear()
    r = await web.op_cancel("m.1", user=yana)
    check(any(uid == 1 and "Вашу запись отменил(а) Яна" in t for uid, t, _ in sent), "автору чужой еды пришло уведомление", sent)
    r = await web.delete_transaction(1, user=alex)
    check(r["ok"], "старый адрес удаления траты работает через отмену", r)
    try:
        await web.op_cancel("t.1 or 1=1", user=alex); check(False, "мусорная ссылка → 400")
    except HTTPException as e:
        check(e.status_code == 400, "мусорная ссылка → 400")
    h = await web.history_data(type="finance", user_id=None, days=30, cancelled=0, user=alex)
    check(all(not o["cancelled"] for o in h["ops"]) and all(o["type"] == "finance" for o in h["ops"]), "фильтры тип + без отменённых")
    try:
        await web.history_data(type="all", user_id=3, days=30, cancelled=1, user=alex); check(False, "чужой user_id → 404")
    except HTTPException as e:
        check(e.status_code == 404, "чужой user_id → 404")
    hb = await web.habits_data(days=30, user_id=None, user=alex)
    check(len(hb["entries"]) == 2 and hb["names"]["water"] == "💧 Вода", "привычки: список записей для кнопок ✕", hb["entries"])
    print("ИТОГ:", "всё прошло" if not FAILS else FAILS)
asyncio.run(main())
