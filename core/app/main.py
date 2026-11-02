"""Ядро ассистента: HTTP API для каналов (Telegram и др.), n8n и будущего веб-интерфейса.

Документация API открывается в браузере: http://127.0.0.1:8100/docs (на самом сервере).
"""
import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import alice, analytics, brain, mobile, release, site, budgets, db, digest, finance, jobs, notify, shopping, ticktick, users, web, web_life
from .config import get_settings
from .schemas import Action, Inbound, Reply
from .util import esc, today

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for _noisy in ("httpx", "httpcore"):   # их INFO-записи содержат адреса вместе с токеном бота
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("core")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    await db.connect(s.database_url)
    await db.migrate()
    await users.sync_allowed_users()
    try:
        await release.announce_if_updated()      # «Ассистент обновлён до …» — один раз после обновления
    except Exception:
        log.exception("Объявление о новой версии")
    stop = asyncio.Event()
    worker = asyncio.create_task(jobs.loop(stop))
    log.info("Ядро %s запущено. Пользователей в ALLOWED_USERS: %d", release.VERSION, len(s.allowed()))
    yield
    stop.set()
    await worker
    await db.close()


app = FastAPI(title="Личный ассистент: ядро", version=release.VERSION, lifespan=lifespan)
app.include_router(web.router)      # веб-панель /web (вход через бота)
app.include_router(web_life.router) # панель: платежи, выписки, гараж, дом, лекарства (1.5)
app.include_router(mobile.router)   # чат /web/chat для панели и Android-приложения (1.7.1)
app.include_router(alice.router)    # Яндекс Алиса /alice/<секрет>
app.include_router(site.router)     # сайт проекта: / и /site/* (1.7.2)


def api_key(x_api_key: str = Header(default="")) -> None:
    if not hmac.compare_digest(x_api_key.encode(), get_settings().core_api_key.encode()):
        raise HTTPException(status_code=401, detail="Неверный X-API-Key")


@app.get("/health")
async def health():
    await db.get_pool().fetchval("SELECT 1")
    return {"status": "ok"}


# ---------- Каналы ----------

@app.post("/v1/inbound", response_model=Reply, dependencies=[Depends(api_key)])
async def inbound(msg: Inbound):
    return await brain.handle_inbound(msg)


@app.post("/v1/action", response_model=Reply, dependencies=[Depends(api_key)])
async def action(act: Action):
    return await brain.handle_action(act)


class NotifyIn(BaseModel):
    text: str
    user_id: int | None = None     # пусто = всем членам семьи


@app.post("/v1/notify", dependencies=[Depends(api_key)])
async def notify_users(body: NotifyIn):
    """Отправить сообщение пользователю от имени ассистента (например, из n8n)."""
    if body.user_id:
        targets = [body.user_id]
    else:
        fam = await db.get_pool().fetchval("SELECT id FROM families ORDER BY id LIMIT 1")
        targets = [u.id for u in await users.family_members(fam)]
    sent = [uid for uid in targets if await notify.send(uid, Reply(text=body.text))]
    return {"sent_to": sent}


# ---------- Данные (для n8n и веб-интерфейса) ----------

@app.get("/v1/users", dependencies=[Depends(api_key)])
async def list_users():
    rows = await db.get_pool().fetch(
        "SELECT u.id, u.name, u.kcal_goal, jsonb_agg(jsonb_build_object('channel', i.channel, 'id', i.external_id))"
        " AS identities FROM users u LEFT JOIN user_identities i ON i.user_id=u.id GROUP BY u.id ORDER BY u.id")
    return [dict(r) for r in rows]


@app.get("/v1/categories", dependencies=[Depends(api_key)])
async def list_categories():
    return await finance.categories()


@app.get("/v1/transactions", dependencies=[Depends(api_key)])
async def list_transactions(date_from: date | None = None, date_to: date | None = None,
                            limit: int = Query(500, le=5000)):
    d2 = date_to or today()
    d1 = date_from or d2.replace(day=1)
    rows = await db.get_pool().fetch(
        "SELECT t.id, t.occurred_on, t.kind, t.amount, t.currency, c.name AS category, t.merchant, t.note,"
        " u.name AS user_name, t.source, t.group_id, t.created_at FROM transactions t"
        " JOIN categories c ON c.id=t.category_id JOIN users u ON u.id=t.user_id"
        " WHERE t.deleted_at IS NULL AND t.occurred_on BETWEEN $1 AND $2"
        " ORDER BY t.occurred_on DESC, t.id DESC LIMIT $3", d1, d2, limit)
    return [dict(r) for r in rows]


class TransactionIn(BaseModel):
    user_id: int
    kind: str = "expense"
    amount: float
    category: str = "Прочее"
    merchant: str | None = None
    note: str | None = None
    occurred_on: date | None = None


@app.post("/v1/transactions", dependencies=[Depends(api_key)])
async def create_transaction(body: TransactionIn):
    """Добавить запись извне (например, n8n разобрал SMS из банка)."""
    user = await users.get(body.user_id)
    if not user:
        raise HTTPException(404, "Нет такого пользователя")
    item = body.model_dump()
    item["date"] = (body.occurred_on or today()).isoformat()
    rows = await finance.save(user, [item], source="api")
    await budgets.check_after_save(user, rows)
    return rows


@app.get("/v1/finance/summary", dependencies=[Depends(api_key)])
async def finance_summary(month: str | None = Query(None, description="YYYY-MM")):
    d = today()
    y, m = (int(month[:4]), int(month[5:7])) if month else (d.year, d.month)
    start = date(y, m, 1)
    end = date(y + (m == 12), m % 12 + 1, 1)
    rows = await db.get_pool().fetch(
        "SELECT t.kind, c.name AS category, u.name AS user_name, sum(t.amount) AS total, count(*) AS n"
        " FROM transactions t JOIN categories c ON c.id=t.category_id JOIN users u ON u.id=t.user_id"
        " WHERE t.deleted_at IS NULL AND t.occurred_on >= $1 AND t.occurred_on < $2"
        " GROUP BY t.kind, c.name, u.name ORDER BY t.kind, total DESC", start, end)
    return {"month": f"{y:04d}-{m:02d}", "rows": [dict(r) for r in rows]}


@app.get("/v1/meals", dependencies=[Depends(api_key)])
async def list_meals(date_from: date | None = None, date_to: date | None = None, user_id: int | None = None):
    d2 = date_to or today()
    d1 = date_from or d2
    tz = get_settings().tz
    rows = await db.get_pool().fetch(
        "SELECT m.id, m.user_id, u.name AS user_name, m.eaten_at, m.meal_type, m.title, m.grams,"
        " m.kcal, m.protein, m.fat, m.carbs, m.source FROM meals m JOIN users u ON u.id=m.user_id"
        " WHERE m.deleted_at IS NULL AND (m.eaten_at AT TIME ZONE $3)::date BETWEEN $1 AND $2"
        " AND ($4::int IS NULL OR m.user_id=$4) ORDER BY m.eaten_at", d1, d2, tz, user_id)
    return [dict(r) for r in rows]


@app.get("/v1/tasks", dependencies=[Depends(api_key)])
async def list_tasks(status: str = "open"):
    rows = await db.get_pool().fetch(
        "SELECT t.id, t.title, t.notes, t.due_at, t.all_day, t.status, u.name AS user_name,"
        " a.name AS assignee_name, t.ticktick_id"
        " FROM tasks t JOIN users u ON u.id=t.user_id LEFT JOIN users a ON a.id=t.assignee_id"
        " WHERE t.status=$1 ORDER BY t.due_at NULLS LAST, t.id", status)
    return [dict(r) for r in rows]


async def _user_or_404(user_id: int) -> users.User:
    user = await users.get(user_id)
    if not user:
        raise HTTPException(404, "Нет такого пользователя")
    return user


@app.get("/v1/shopping", dependencies=[Depends(api_key)])
async def list_shopping():
    fam = await db.get_pool().fetchval("SELECT id FROM families ORDER BY id LIMIT 1")
    return await shopping.open_items(fam)


class ShoppingIn(BaseModel):
    user_id: int
    items: list[str]


@app.post("/v1/shopping", dependencies=[Depends(api_key)])
async def add_shopping(body: ShoppingIn):
    user = await _user_or_404(body.user_id)
    return await shopping.add(user, [{"title": t} for t in body.items])


@app.get("/v1/budgets", dependencies=[Depends(api_key)])
async def list_budgets():
    fam = await db.get_pool().fetchval("SELECT id FROM families ORDER BY id LIMIT 1")
    return await budgets.status_rows(fam)


class AskIn(BaseModel):
    user_id: int
    question: str


@app.post("/v1/ask", response_model=Reply, dependencies=[Depends(api_key)])
async def ask(body: AskIn):
    """Вопрос к данным обычным языком (то же, что в Telegram). Удобно для n8n и веб-интерфейса."""
    return await analytics.answer(await _user_or_404(body.user_id), body.question)


@app.post("/v1/digest/{user_id}", response_model=Reply, dependencies=[Depends(api_key)])
async def weekly_digest(user_id: int, send: bool = False):
    """Недельный разбор. send=true — ещё и отправить пользователю."""
    user = await _user_or_404(user_id)
    reply = await digest.build(user)
    if send:
        await notify.send(user.id, reply)
    return reply


# ---------- TickTick: возврат после входа ----------

PAGE = ("<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'></head>"
        "<body style='font-family:sans-serif;padding:32px'><h2>{title}</h2><p>{text}</p></body></html>")


@app.get("/ticktick/callback", response_class=HTMLResponse)
async def ticktick_callback(code: str = "", state: str = ""):
    try:
        uid = await ticktick.exchange_code(code, state)
    except ticktick.TickTickError as e:
        return PAGE.format(title="Не получилось", text=esc(str(e)))
    await notify.send(uid, urgent=True, reply=Reply(text="✅ TickTick подключён. Новые задачи будут появляться во «Входящих»."))
    return PAGE.format(title="Готово ✅", text="TickTick подключён. Можно вернуться в Telegram.")
