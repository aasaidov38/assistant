"""Веб-панель: графики и таблицы. Вход только через бота.

Как входить:
  1) в боте команда /web → бот присылает одноразовую ссылку (действует 10 минут);
  2) по ссылке открывается страница с кнопкой «Войти» → браузер получает защищённую cookie на WEB_SESSION_DAYS дней;
  3) о каждом входе бот пишет в Telegram. Выйти везде: /web off.
Пароли нигде не хранятся, в базе лежат только хеши ссылок и сессий.
"""
import hashlib
import re
import secrets
from datetime import date, datetime, time, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import analytics, budgets, db, finance, habits, morning, notify, nutrition, scope, shopping, tasks, ticktick, undo, users
from .config import get_settings
from .schemas import Button, Reply
from .util import esc, today

router = APIRouter()
COOKIE = "assistant_session"
LOGIN_TTL = timedelta(minutes=10)
INDEX = Path(__file__).resolve().parent / "static" / "index.html"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------- Вход через бота ----------

async def login_link(user: users.User) -> Reply:
    s = get_settings()
    if not s.public_url:
        return Reply(text="🌐 Для веб-панели нужен публичный адрес: заполните PUBLIC_URL в .env "
                          "(README, раздел «Публичный адрес через Caddy»).")
    token = secrets.token_urlsafe(32)
    await db.get_pool().execute(
        "INSERT INTO web_login_tokens (token_hash, user_id, expires_at) VALUES ($1,$2,now()+$3::interval)",
        _hash(token), user.id, LOGIN_TTL)
    url = f"{s.public_url.rstrip('/')}/web/login?t={token}"
    return Reply(text="🌐 <b>Вход в веб-панель</b>\n\nНажмите «Открыть панель», затем «Войти».\n"
                      "Кнопка одноразовая и действует 10 минут. Никому её не пересылайте.\n"
                      "Выйти на всех устройствах: <code>/web off</code>",
                 buttons=[[Button(text="🌐 Открыть панель", url=url)],
                          [Button(text="🔒 Выйти везде", action="w:0:logout")]])


async def logout_all(user: users.User) -> Reply:
    n = await db.get_pool().fetchval(
        "WITH d AS (DELETE FROM web_sessions WHERE user_id=$1 RETURNING 1) SELECT count(*) FROM d", user.id)
    return Reply(text=f"🔒 Выход выполнен на всех устройствах (закрыто сессий: {n}).")


async def handle_action(user: users.User, parts: list[str]) -> Reply:
    if parts[2] == "logout":
        return await logout_all(user)
    if parts[2] == "login":
        return await login_link(user)
    return Reply(text="Неизвестное действие.")


PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Семейный ассистент</title>
<style>body{{font-family:system-ui,sans-serif;background:#f4f5f7;color:#1d2330;display:flex;min-height:100vh;
align-items:center;justify-content:center;margin:0;padding:16px}}.c{{background:#fff;border-radius:16px;padding:32px;
max-width:380px;box-shadow:0 4px 24px #0001;text-align:center}}button{{font:inherit;background:#2f6fed;color:#fff;
border:0;border-radius:10px;padding:12px 28px;cursor:pointer;font-size:17px}}p{{color:#5b6475;line-height:1.5}}
@media(prefers-color-scheme:dark){{body{{background:#14171c;color:#e8eaee}}.c{{background:#1e232b}}p{{color:#a3abb9}}}}
</style></head><body><div class="c"><h2>{title}</h2>{body}</div></body></html>"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(PAGE.format(title=title, body=body), status_code=status,
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                 "Referrer-Policy": "no-referrer"})


NEED_LOGIN = ("<p>Чтобы войти, отправьте боту команду <b>/web</b> и откройте ссылку из ответа.</p>")


@router.get("/web/login", response_class=HTMLResponse)
async def login_page(t: str = ""):
    # Ссылка не тратится при открытии: её могут заранее открыть программы предпросмотра.
    # Вход происходит только по нажатию кнопки.
    if not re.fullmatch(r"[\w-]{20,100}", t):
        return _page("Ссылка неверная", NEED_LOGIN, 400)
    ok = await db.get_pool().fetchval(
        "SELECT 1 FROM web_login_tokens WHERE token_hash=$1 AND used_at IS NULL AND expires_at > now()", _hash(t))
    if not ok:
        return _page("Ссылка устарела", NEED_LOGIN, 400)
    return _page("Семейный ассистент", f"""<p>Войти в веб-панель на этом устройстве?</p>
<form method="post" action="/web/login"><input type="hidden" name="t" value="{esc(t)}">
<button type="submit">Войти</button></form>""")


@router.post("/web/login")
async def login(request: Request, t: str = Form("")):
    s = get_settings()
    async with db.get_pool().acquire() as conn, conn.transaction():
        uid = await conn.fetchval(
            "UPDATE web_login_tokens SET used_at=now() WHERE token_hash=$1 AND used_at IS NULL AND expires_at > now()"
            " RETURNING user_id", _hash(t))
        if not uid:
            return _page("Ссылка устарела", NEED_LOGIN, 400)
        session = secrets.token_urlsafe(32)
        ua = (request.headers.get("user-agent") or "")[:300]
        await conn.execute(
            "INSERT INTO web_sessions (token_hash, user_id, expires_at, user_agent) VALUES ($1,$2,now()+$3::interval,$4)",
            _hash(session), uid, timedelta(days=s.web_session_days), ua)
    await notify.send(uid, urgent=True, reply=Reply(text=f"🔐 Выполнен вход в веб-панель.\n<i>{esc(_device(ua))}</i>\n"
                                      "Если это были не вы: <code>/web off</code>",
                                 buttons=[[Button(text="🔒 Выйти везде", action="w:0:logout")]]))
    resp = RedirectResponse("/web/", status_code=303)
    resp.set_cookie(COOKIE, session, max_age=s.web_session_days * 86400, httponly=True,
                    secure=request.url.scheme == "https", samesite="lax", path="/web")
    return resp


def _device(ua: str) -> str:
    os_ = next((n for k, n in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"),
                                ("Mac OS", "Mac"), ("Linux", "Linux")) if k in ua), "устройство")
    br = next((n for k, n in (("YaBrowser", "Яндекс Браузер"), ("Edg/", "Edge"), ("Firefox", "Firefox"),
                               ("Chrome", "Chrome"), ("Safari", "Safari")) if k in ua), "браузер")
    return f"{br}, {os_}"


@router.post("/web/logout")
async def logout(request: Request):
    tok = request.cookies.get(COOKIE)
    if tok:
        await db.get_pool().execute("DELETE FROM web_sessions WHERE token_hash=$1", _hash(tok))
    resp = RedirectResponse("/web/", status_code=303)
    resp.delete_cookie(COOKIE, path="/web")
    return resp


async def _session_user(request: Request) -> users.User | None:
    tok = request.cookies.get(COOKIE)
    if not tok:
        return None
    uid = await db.get_pool().fetchval(
        "UPDATE web_sessions SET last_seen=now() WHERE token_hash=$1 AND expires_at > now() RETURNING user_id",
        _hash(tok))
    return await users.get(uid) if uid else None


async def current_user(request: Request) -> users.User:
    user = await _session_user(request)
    if not user:
        raise HTTPException(401, "Нужно войти через бота: команда /web")
    if request.method != "GET" and request.headers.get("x-requested-with") != "panel":
        raise HTTPException(403, "Запрос не из панели")      # защита от подделки запросов с чужих сайтов
    await scope.enter(user)          # личные и общие разделы: что этому человеку видно
    return user


# Файлы панели, которые лежат рядом (без внешних сайтов): Chart.js 4.4.1 (MIT), генератор QR Kazuhiko Arase (MIT),
# иконки приложения. Только этот список — никаких произвольных путей.
STATIC = {"chart.umd.js": "text/javascript", "qrcode.js": "text/javascript", "icon-192.png": "image/png",
          "icon-512.png": "image/png", "icon-maskable-512.png": "image/png", "apple-touch-icon.png": "image/png"}


@router.get("/web/static/{name}")
async def static_file(name: str):
    if name not in STATIC:
        raise HTTPException(404)
    return FileResponse(INDEX.parent / name, media_type=STATIC[name], headers={"Cache-Control": "public, max-age=604800"})


MANIFEST = {
    "name": "Семейный ассистент", "short_name": "Ассистент", "lang": "ru", "dir": "ltr",
    "description": "Траты, питание, дела, покупки и напоминания семьи",
    "id": "/web/", "start_url": "/web/", "scope": "/web/", "display": "standalone",
    "background_color": "#f3f4f8", "theme_color": "#3b82f6",
    "icons": [{"src": "/web/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "/web/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
              {"src": "/web/static/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
    "shortcuts": [{"name": "Чат", "url": "/web/chat", "icons": [{"src": "/web/static/icon-192.png", "sizes": "192x192"}]},
                  {"name": "Карты лояльности", "url": "/web/#cards", "icons": [{"src": "/web/static/icon-192.png", "sizes": "192x192"}]}],
}


@router.get("/web/manifest.webmanifest")
async def manifest():
    return JSONResponse(MANIFEST, media_type="application/manifest+json", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/web/sw.js")
async def service_worker():
    return FileResponse(INDEX.parent / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@router.get("/web")
async def web_root():
    return RedirectResponse("/web/", status_code=307)


@router.get("/web/", response_class=HTMLResponse)
async def index(request: Request):
    if not await _session_user(request):
        return _page("Семейный ассистент", NEED_LOGIN, 401)
    return HTMLResponse(INDEX.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})


# ---------- Данные для панели ----------

def _month_bounds(month: str | None) -> tuple[date, date]:
    d = today()
    if month and re.fullmatch(r"\d{4}-\d{2}", month):
        d = date(int(month[:4]), int(month[5:]), 1)
    a = d.replace(day=1)
    return a, date(a.year + (a.month == 12), a.month % 12 + 1, 1)


def _f(v):
    return float(v) if v is not None else None


@router.get("/web/api/me")
async def me(user: users.User = Depends(current_user)):
    from . import prefs
    members = await users.family_members(user.family_id)
    s = await prefs.get(user.id)
    return {"id": user.id, "name": user.name, "family": [{"id": m.id, "name": m.name} for m in members],
            "currency": get_settings().currency, "owner": members[0].id == user.id if members else False,
            "modes": await scope.modes(user.family_id),
            "ui": {"theme": s["theme"], "animations": s["animations"], "hidden_tabs": s["hidden_tabs"]}}


@router.get("/web/api/finance")
async def finance_data(month: str | None = None, user: users.User = Depends(current_user)):
    a, b = _month_bounds(month)
    pool = db.get_pool()
    base = " FROM transactions t JOIN categories c ON c.id=t.category_id JOIN users u ON u.id=t.user_id" \
           " WHERE t.family_id=$1 AND t.deleted_at IS NULL AND t.occurred_on >= $2 AND t.occurred_on < $3" \
           f" AND {scope.cond('finance', 't.user_id')}"
    cats = await pool.fetch("SELECT t.kind, c.name, c.emoji, sum(t.amount) AS s" + base +
                            " GROUP BY 1,2,3 ORDER BY 4 DESC", user.family_id, a, b)
    daily = await pool.fetch("SELECT t.occurred_on AS d, sum(t.amount) AS s" + base +
                             " AND t.kind='expense' GROUP BY 1 ORDER BY 1", user.family_id, a, b)
    people = await pool.fetch("SELECT u.name, t.kind, sum(t.amount) AS s" + base + " GROUP BY 1,2",
                              user.family_id, a, b)
    recent = await pool.fetch("SELECT t.id, t.occurred_on, t.kind, t.amount, c.name AS category, c.emoji, t.merchant,"
                              " t.note, u.name AS user_name, t.source" + base +
                              " ORDER BY t.occurred_on DESC, t.id DESC LIMIT 200", user.family_id, a, b)
    # Расходы по месяцам за полгода (для графика динамики)
    six = date(a.year - (a.month <= 5), (a.month - 6) % 12 + 1, 1)
    months = await pool.fetch(
        "SELECT to_char(occurred_on, 'YYYY-MM') AS m, kind, sum(amount) AS s FROM transactions"
        " WHERE family_id=$1 AND deleted_at IS NULL AND occurred_on >= $2 AND occurred_on < $3"
        f" AND {scope.cond('finance', 'user_id')} GROUP BY 1,2 ORDER BY 1",
        user.family_id, six, b)
    return {
        "month": f"{a:%Y-%m}",
        "income": sum(_f(r["s"]) for r in cats if r["kind"] == "income"),
        "expense": sum(_f(r["s"]) for r in cats if r["kind"] == "expense"),
        "categories": [{"kind": r["kind"], "name": r["name"], "emoji": r["emoji"], "sum": _f(r["s"])} for r in cats],
        "daily": [{"date": r["d"].isoformat(), "sum": _f(r["s"])} for r in daily],
        "people": [{"name": r["name"], "kind": r["kind"], "sum": _f(r["s"])} for r in people],
        "months": [{"month": r["m"], "kind": r["kind"], "sum": _f(r["s"])} for r in months],
        "budgets": [{"name": r["name"], "emoji": r["emoji"], "limit": _f(r["lim"]), "spent": _f(r["spent"])}
                    for r in await budgets.status_rows(user.family_id, a)],
        "groups": [{"name": g["grp"], "sum": _f(g["total"])} for g in await finance.item_groups(user.family_id, a, b, 12)],
        "transactions": [{**dict(r), "amount": _f(r["amount"]), "occurred_on": r["occurred_on"].isoformat()}
                         for r in recent],
    }


@router.post("/web/api/transactions/{tx_id}/delete")
async def delete_transaction(tx_id: int, user: users.User = Depends(current_user)):
    # старый адрес, оставлен для совместимости: теперь всё идёт через общую отмену
    return await op_cancel(f"t.{tx_id}", user)


# ---------- История и отмена записей ----------

def _op_json(op: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in op.items() if k != "search"}


@router.get("/web/api/history")
async def history_data(type: str = "all", user_id: int | None = None, days: int = 30, cancelled: int = 1,
                       user: users.User = Depends(current_user)):
    if user_id:
        await _member(user, user_id)          # только члены своей семьи
    ops = await undo.history(user.family_id, user_id=user_id or None,
                             types=[type] if type in undo.TYPES else None, days=days, limit=300)
    if not cancelled:
        ops = [o for o in ops if not o["cancelled"]]
    return {"types": undo.TYPES, "ops": [_op_json(o) for o in ops]}


async def _tell_owner(user: users.User, op: dict, verb: str) -> None:
    """Если отменили чужую личную запись (еда, привычки), сообщим её автору."""
    if op["type"] in ("meal", "metric") and op["user_id"] and op["user_id"] != user.id:
        try:
            await notify.send(op["user_id"], Reply(text=f"{verb} {esc(user.name)} в веб-панели: "
                                                        f"{undo.describe(op)}"))
        except Exception:
            pass


@router.post("/web/api/ops/{ref}/cancel")
async def op_cancel(ref: str, user: users.User = Depends(current_user)):
    if not undo.valid_ref(ref):
        raise HTTPException(400, "Неверная ссылка на запись")
    op, note = await undo.cancel(user, ref, via="web")
    if not op:
        raise HTTPException(409, note)
    await _mirror(user, f"🗑 Отменено: {undo.describe(op, user)}" + (f"\n{note}" if note else ""),
                  [[Button(text="↩️ Вернуть", action=f"u:{ref}:back")]] if op["can_restore"] else None)
    await _tell_owner(user, op, "🗑 Вашу запись отменил(а)")
    return {"ok": True, "op": _op_json(op), "text": undo.describe(op, user, html=False), "note": note}


@router.post("/web/api/ops/{ref}/restore")
async def op_restore(ref: str, user: users.User = Depends(current_user)):
    if not undo.valid_ref(ref):
        raise HTTPException(400, "Неверная ссылка на запись")
    op, note = await undo.restore(user, ref, via="web")
    if not op:
        raise HTTPException(409, note)
    await _mirror(user, f"↩️ Возвращено: {undo.describe(op, user)}",
                  [[Button(text="❌ Отменить", action=f"u:{ref}:del")]])
    await _tell_owner(user, op, "↩️ Вашу запись вернул(а)")
    return {"ok": True, "op": _op_json(op), "text": undo.describe(op, user, html=False)}


async def _mirror(user: users.User, text: str, buttons=None) -> None:
    """Копия действия из веб-панели в Telegram, чтобы история была в одном месте."""
    try:
        from . import prefs
        if (await prefs.get(user.id))["mirror"]:
            await notify.send(user.id, Reply(text=f"🌐 <i>Веб-панель</i>\n{text}", buttons=buttons or []), quiet="drop")
    except Exception:
        pass


async def _member(user: users.User, user_id: int | None, section: str | None = None) -> users.User:
    """Чьи данные смотреть. В личном разделе — только свои."""
    if not user_id or user_id == user.id:
        return user
    m = await users.get(user_id)
    if not m or m.family_id != user.family_id:
        raise HTTPException(404, "Нет такого члена семьи")
    if section and scope.is_personal(section):
        raise HTTPException(403, "Раздел личный: видно только свои записи")
    return m


def _day_bounds(d: date):
    z = get_settings().zone
    a = datetime.combine(d, time.min, tzinfo=z)
    return a, a + timedelta(days=1)


@router.get("/web/api/nutrition")
async def nutrition_data(days: int = 30, user_id: int | None = None, user: users.User = Depends(current_user)):
    m = await _member(user, user_id, "nutrition")
    days = max(7, min(days, 365))
    start = today() - timedelta(days=days - 1)
    rows = await db.get_pool().fetch(
        "SELECT (eaten_at AT TIME ZONE $3)::date AS d, sum(kcal) AS kcal, sum(protein) AS p, sum(fat) AS f,"
        " sum(carbs) AS c FROM meals WHERE user_id=$1 AND deleted_at IS NULL AND eaten_at >= $2 GROUP BY 1 ORDER BY 1",
        m.id, _day_bounds(start)[0], get_settings().tz)
    a, b = _day_bounds(today())
    meals = await db.get_pool().fetch(
        "SELECT id, eaten_at, meal_type, title, kcal, protein, fat, carbs FROM meals WHERE user_id=$1"
        " AND deleted_at IS NULL AND eaten_at >= $2 ORDER BY eaten_at DESC LIMIT 60",
        m.id, a - timedelta(days=6))
    return {"name": m.name, "goal": nutrition.goal_for(m),
            "days": [{"date": r["d"].isoformat(), "kcal": _f(r["kcal"]), "protein": _f(r["p"]), "fat": _f(r["f"]),
                      "carbs": _f(r["c"])} for r in rows],
            "meals": [{"id": r["id"], "at": r["eaten_at"].isoformat(), "type": r["meal_type"], "title": r["title"],
                       "kcal": _f(r["kcal"]), "protein": _f(r["protein"]), "fat": _f(r["fat"]),
                       "carbs": _f(r["carbs"])} for r in meals]}


@router.get("/web/api/habits")
async def habits_data(days: int = 30, user_id: int | None = None, user: users.User = Depends(current_user)):
    m = await _member(user, user_id, "habits")
    days = max(7, min(days, 365))
    start = today() - timedelta(days=days - 1)
    rows = await db.get_pool().fetch(
        "SELECT kind, (measured_at AT TIME ZONE $3)::date AS d, sum(value) AS s, max(value) AS mx,"
        " (array_agg(value ORDER BY measured_at DESC))[1] AS last FROM metrics"
        " WHERE user_id=$1 AND deleted_at IS NULL AND measured_at >= $2 GROUP BY 1,2 ORDER BY 2",
        m.id, _day_bounds(start)[0], get_settings().tz)
    series: dict[str, list] = {k: [] for k in habits.KINDS}
    for r in rows:
        agg = habits.KINDS[r["kind"]][4]
        v = r["s"] if agg == "sum" else r["mx"] if agg == "max" else r["last"]
        series[r["kind"]].append({"date": r["d"].isoformat(), "value": _f(v)})
    g = await habits.goals(m.id)
    entries = await db.get_pool().fetch(
        "SELECT id, kind, value, measured_at FROM metrics WHERE user_id=$1 AND deleted_at IS NULL"
        " ORDER BY measured_at DESC, id DESC LIMIT 40", m.id)
    return {"name": m.name, "series": series, "goals": {k: _f(v) for k, v in g.items()},
            "units": {k: v[2] for k, v in habits.KINDS.items()},
            "names": {k: f"{v[0]} {v[1]}" for k, v in habits.KINDS.items()},
            "entries": [{"id": r["id"], "kind": r["kind"], "value": _f(r["value"]), "at": r["measured_at"].isoformat()}
                        for r in entries]}


@router.get("/web/api/tasks")
async def tasks_data(done_days: int = 7, user: users.User = Depends(current_user)):
    pool = db.get_pool()
    q = ("SELECT t.id, t.title, t.notes, t.due_at, t.all_day, t.status, t.done_at, coalesce(a.name, u.name) AS owner,"
         " coalesce(t.assignee_id, t.user_id) AS owner_id, u.name AS author, t.ticktick_id IS NOT NULL AS in_ticktick,"
         " t.ticktick_list, t.source FROM tasks t JOIN users u ON u.id=t.user_id LEFT JOIN users a ON a.id=t.assignee_id"
         " WHERE t.family_id=$1 AND ")
    open_ = await pool.fetch(q + "t.status='open' ORDER BY t.due_at NULLS LAST, t.id LIMIT 300", user.family_id)
    done = await pool.fetch(q + "t.status='done' AND t.done_at > now() - make_interval(days => $2)"
                                " ORDER BY t.done_at DESC LIMIT 50", user.family_id, max(1, min(done_days, 60)))
    conv = lambda r: {**dict(r), "due_at": r["due_at"].isoformat() if r["due_at"] else None,
                      "done_at": r["done_at"].isoformat() if r["done_at"] else None}
    return {"open": [conv(r) for r in open_], "done": [conv(r) for r in done]}


class TaskIn(BaseModel):
    title: str
    date: str | None = None        # YYYY-MM-DD
    time: str | None = None        # HH:MM
    notes: str | None = None
    assignee_id: int | None = None
    remind_minutes: int | None = 15


@router.post("/web/api/tasks")
async def task_create(body: TaskIn, user: users.User = Depends(current_user)):
    title = body.title.strip()[:300]
    if not title:
        raise HTTPException(400, "Пустое название")
    due = None
    if body.date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", body.date):
        due = body.date + (f"T{body.time}" if body.time and re.fullmatch(r"\d{2}:\d{2}", body.time) else "")
    assignee = None
    if body.assignee_id and body.assignee_id != user.id:
        assignee = (await _member(user, body.assignee_id)).name
    task, note = await tasks.save(user, {"title": title, "notes": (body.notes or "").strip() or None, "due": due,
                                         "assignee": assignee,
                                         "remind_minutes_before": body.remind_minutes if body.time else None},
                                  source="web")
    reply = tasks.render_saved(task, note)
    await _mirror(user, reply.text, reply.buttons)
    return {"id": task["id"], "note": re.sub(r"<[^>]+>", "", note)}


@router.post("/web/api/tasks/{task_id}/delete")
async def task_delete(task_id: int, user: users.User = Depends(current_user)):
    reply = await tasks.handle_action(user, ["k", str(task_id), "del"])
    await _mirror(user, reply.text)
    return {"text": reply.text}


@router.get("/web/api/ticktick")
async def ticktick_status(user: users.User = Depends(current_user)):
    out = {"configured": ticktick.configured()}
    for m in await users.family_members(user.family_id):
        info = await ticktick.token_info(m.id)
        out.setdefault("members", []).append({"id": m.id, "name": m.name, **info})
    return out


@router.post("/web/api/ticktick/sync")
async def ticktick_sync_now(user: users.User = Depends(current_user)):
    reply = await tasks.sync_now(user)
    return {"text": re.sub(r"<[^>]+>", "", reply.text.split("\n\n")[0])}


@router.get("/web/api/weather")
async def weather(user: users.User = Depends(current_user)):
    data = await morning.forecast()
    if not data:
        raise HTTPException(503, "Погода сейчас недоступна")
    return data


@router.get("/web/api/home")
async def home(user: users.User = Depends(current_user)):
    """Сводка на сегодня для главной вкладки."""
    d = today()
    a, b = _day_bounds(d)
    pool = db.get_pool()
    t_rows = await pool.fetch(
        "SELECT id, title, due_at, all_day, ticktick_id IS NOT NULL AS in_ticktick FROM tasks WHERE family_id=$1"
        " AND status='open' AND coalesce(assignee_id, user_id)=$2 AND due_at < $3 ORDER BY due_at", user.family_id, user.id, b)
    tot = await nutrition.day_totals(user, d)
    hab = {}
    g = await habits.goals(user.id)
    for kind in ("water", "steps", "sleep"):
        hab[kind] = {"value": _f(await habits.day_value(user.id, kind, d)), "goal": _f(g.get(kind)),
                     "unit": habits.KINDS[kind][2]}
    w = await habits.last_weight(user.id)
    wk = await habits.weight_week_ago(user.id)
    spent, n = await finance.today_expenses(user, d)
    ma, mb = _month_bounds(None)
    month_spent = await pool.fetchval(
        "SELECT coalesce(sum(amount),0) FROM transactions WHERE family_id=$1 AND kind='expense' AND deleted_at IS NULL"
        f" AND occurred_on >= $2 AND occurred_on < $3 AND {scope.cond('finance', 'user_id')}", user.family_id, ma, mb)
    return {
        "date": d.isoformat(),
        "tasks": [{"id": r["id"], "title": r["title"], "due_at": r["due_at"].isoformat(), "all_day": r["all_day"],
                   "overdue": r["due_at"] < a, "in_ticktick": r["in_ticktick"]} for r in t_rows],
        "food": {"kcal": _f(tot["kcal"]), "goal": nutrition.goal_for(user), "protein": _f(tot["protein"]),
                 "fat": _f(tot["fat"]), "carbs": _f(tot["carbs"])},
        "habits": hab, "weight": {"now": _f(w), "week_ago": _f(wk), "goal": _f(g.get("weight"))},
        "money": {"today": _f(spent), "today_n": n, "month": _f(month_spent)},
        "budgets": [{"name": r["name"], "emoji": r["emoji"], "limit": _f(r["lim"]), "spent": _f(r["spent"])}
                    for r in await budgets.status_rows(user.family_id)],
        "shopping": len(await shopping.open_items(user.family_id)),
    }


@router.post("/web/api/tasks/{task_id}/done")
async def task_done(task_id: int, user: users.User = Depends(current_user)):
    reply = await tasks.handle_action(user, ["k", str(task_id), "done"])
    await _mirror(user, reply.text)
    return {"text": reply.text}


@router.get("/web/api/shopping")
async def shopping_data(user: users.User = Depends(current_user)):
    return await shopping.open_items(user.family_id)


class ShopIn(BaseModel):
    title: str


@router.post("/web/api/shopping")
async def shopping_add(body: ShopIn, user: users.User = Depends(current_user)):
    items = [{"title": t.strip()} for t in re.split(r"[,;\n]", body.title) if t.strip()][:30]
    reply = await shopping.add(user, items)
    await _mirror(user, reply.text, reply.buttons)
    return await shopping.open_items(user.family_id)


@router.post("/web/api/shopping/{item_id}/buy")
async def shopping_buy(item_id: int, user: users.User = Depends(current_user)):
    title = await db.get_pool().fetchval(
        "UPDATE shopping_items SET bought_at=now() WHERE id=$1 AND family_id=$2 AND bought_at IS NULL RETURNING title",
        item_id, user.family_id)
    if title:
        await _mirror(user, f"✅ Куплено: {esc(title)}")
    return await shopping.open_items(user.family_id)


class AskIn(BaseModel):
    question: str


@router.post("/web/api/ask")
async def ask(body: AskIn, user: users.User = Depends(current_user)):
    q = body.question.strip()[:500]
    if not q:
        raise HTTPException(400, "Пустой вопрос")
    reply = await analytics.answer(user, q)
    await _mirror(user, f"❓ {esc(q)}\n{reply.text}")
    return {"html": reply.text}      # текст уже экранирован ядром
