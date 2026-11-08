"""Демо-кабинет (версия 1.10.7): публичная песочница для сайта https://demo.assistant.saidov.ru/

Это отдельный экземпляр ядра (сервис `demo` в docker-compose.yml, профиль demo) со своей базой PostgreSQL (`demo-db`).
Реальных данных семьи, ключей Telegram, TickTick и Алисы там нет, а ключ ИИ — отдельный, с лимитами.

Как это устроено:
  * Раз в сутки (и при запуске) собирается ЗАГОТОВКА — база `demo_t_…` с вымышленной семьёй и историей за ~6 месяцев (demo_data.py).
  * Посетитель нажимает «Открыть демо» (именно нажимает: предпросмотр ссылок в мессенджерах песочницу не создаёт) —
    для него командой CREATE DATABASE … TEMPLATE копируется заготовка в личную базу `demo_s_…`, открывается сеанс панели.
  * Каждый запрос панели идёт в базу своего посетителя (db.get_pool() отдаёт пул из contextvar, см. DemoMiddleware).
  * Через demo_idle_min минут без действий или demo_max_min минут от начала база удаляется. Всё, что посетитель менял, исчезает.
  * «Начать заново» удаляет базу сразу и создаёт новую. «Войти как …» — тот же сеанс от имени другого члена семьи.
  * ИИ работает по-настоящему, но с лимитами (число сообщений и обращений на сеанс, общий лимит в сутки). Фото, голос, выписки отключены.
  * Фоновых задач (напоминания, Telegram, обновления) в демо нет.
"""
import asyncio
import contextvars
import hashlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from http.cookies import SimpleCookie
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from . import db
from .config import get_settings
from .util import esc, today

log = logging.getLogger("demo")

COOKIE = "demo_sid"
SESSION_COOKIE = "assistant_session"          # та же cookie, что у обычной панели (web.COOKIE)
ALLOWED_EXACT = {"/", "/health", "/robots.txt"}
ALLOWED_PREFIX = ("/demo", "/web")
# что в демо не работает, даже если кнопка видна: ответ 403
DENIED_PREFIX = ("/web/login", "/web/logout", "/web/api/update", "/web/api/bank/upload", "/web/api/push",
                 "/web/api/sizes", "/web/api/balance", "/web/api/ticktick", "/web/api/integrations")
BLOCKED_CMDS = ("/web", "/app", "/alice", "/ticktick", "/update", "/shrink", "/size", "/version")
MAX_TEXT = 400
DB_NAME = re.compile(r"demo_[st]_[a-z0-9]{6,20}")

MSG_LIMIT = ("🧪 В демо можно отправить ИИ-чату {n} сообщений за сеанс, лимит исчерпан. "
             "Всё остальное работает без ограничений: добавляйте траты и дела кнопками в панели. "
             "Или нажмите «Начать заново» вверху.")
MSG_DAY = "🧪 Сегодня демо уже много спрашивали ИИ, лимит на сутки исчерпан. Панель работает как обычно; ИИ-чат вернётся завтра."
MSG_OFF = "🧪 ИИ в демо сейчас выключен. Панель работает как обычно."
MSG_FILES = "🧪 В демо фото, голос и файлы отключены. В своём ассистенте это работает: фото чека разбирается по позициям, голос превращается в запись."


class Busy(Exception):
    def __init__(self, minutes: int):
        super().__init__(f"занято, примерно {minutes} мин")
        self.minutes = minutes


class NotReady(Exception):
    pass


class RateLimited(Exception):
    pass


@dataclass
class Sandbox:
    sid_hash: str
    db_name: str
    pool: object
    created: float
    last: float
    user: int = 1
    ai_msgs: int = 0
    ai_calls: int = 0
    hits: list = field(default_factory=list)


_current: contextvars.ContextVar = contextvars.ContextVar("demo_sandbox", default=None)
_boxes: dict[str, Sandbox] = {}
_ip_hits: dict[str, list] = {}
_ai_day: list = [None, 0]
_clone_lock = asyncio.Lock()
_state = {"template": None, "day": None, "next_try": 0.0, "building": False}


def on() -> bool:
    return bool(get_settings().demo_mode)


def current() -> Sandbox | None:
    return _current.get()


# ---------- подключение к базам ----------

def dsn_for(name: str) -> str:
    """Адрес базы name на том же сервере PostgreSQL, что и управляющая база (DATABASE_URL)."""
    if not DB_NAME.fullmatch(name):
        raise ValueError("недопустимое имя базы")
    u = urlsplit(get_settings().database_url)
    return urlunsplit((u.scheme, u.netloc, "/" + name, u.query, ""))


async def _open_pool(name: str, size: int = 2):
    import asyncpg
    return await asyncpg.create_pool(dsn_for(name), min_size=0, max_size=size, init=db._init_conn,
                                     command_timeout=30, server_settings={"statement_timeout": "20000"})


def _admin():
    """Управляющая база: через неё создаются и удаляются базы посетителей (всегда общий пул, не пул посетителя)."""
    assert db.pool is not None, "База не подключена"
    return db.pool


async def _drop_db(name: str) -> None:
    if not DB_NAME.fullmatch(name):
        return
    try:
        await _admin().execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    except Exception as e:
        log.warning("Не удалось удалить базу %s: %s", name, e)


# ---------- заготовка ----------

async def build_template() -> str:
    """Собирает новую заготовку (миграции + вымышленные данные) и подменяет ею прежнюю."""
    from . import demo_data, market
    if _state["building"]:
        raise NotReady("заготовка уже собирается")
    _state["building"] = True
    name = "demo_t_" + datetime.now().strftime("%m%d%H%M%S")
    t0 = time.time()
    pool = None
    token = None
    try:
        await _admin().execute(f'CREATE DATABASE "{name}"')
        pool = await _open_pool(name, 3)
        token = db._override.set(pool)
        await db.migrate()
        await demo_data.seed(pool)
        for key in ("fx", "crypto", "indices", "macro"):        # курсы и индексы — настоящие, без ключей; нет сети — карточки скрыты
            try:
                await asyncio.wait_for(market.refresh(key), 30)
            except Exception as e:
                log.warning("Рынок в заготовке, %s: %s", key, e)
    except Exception:
        if token is not None:
            db._override.reset(token)
            token = None
        if pool is not None:
            await pool.close()
            pool = None
        await _drop_db(name)
        _state["building"] = False
        raise
    finally:
        if token is not None:
            db._override.reset(token)
        if pool is not None:
            await pool.close()
    async with _clone_lock:
        old, _state["template"], _state["day"] = _state["template"], name, today()
    _state["building"] = False
    if old:
        async with _clone_lock:
            await _drop_db(old)
    log.info("Заготовка демо %s собрана за %.1f с", name, time.time() - t0)
    return name


# ---------- сеансы ----------

def _client_ip(headers: dict) -> str:
    xf = (headers.get("x-forwarded-for") or "").split(",")
    return (xf[-1].strip() if xf and xf[-1].strip() else headers.get("x-real-ip") or "?")[:64]


def _rate_ok(ip: str) -> bool:
    s = get_settings()
    now = time.time()
    hits = [t for t in _ip_hits.get(ip, []) if now - t < 3600]
    _ip_hits[ip] = hits
    if len(hits) >= s.demo_new_per_hour:
        return False
    hits.append(now)
    return True


def _minutes_until_free() -> int:
    s = get_settings()
    now = time.time()
    left = [min(b.created + s.demo_max_min * 60, b.last + s.demo_idle_min * 60) - now for b in _boxes.values()]
    return max(1, int(min(left) // 60) + 1) if left else 1


async def make_session(box: Sandbox, user_id: int, user_agent: str = "") -> str:
    """Новая сессия панели в базе посетителя от имени члена семьи user_id (возвращает значение cookie)."""
    ok = await box.pool.fetchval("SELECT 1 FROM users WHERE id=$1", user_id)
    if not ok:
        raise ValueError("нет такого пользователя")
    token = secrets.token_urlsafe(32)
    await box.pool.execute("DELETE FROM web_sessions")
    await box.pool.execute(
        "INSERT INTO web_sessions (token_hash, user_id, expires_at, user_agent) VALUES ($1,$2,now()+interval '3 hours',$3)",
        hashlib.sha256(token.encode()).hexdigest(), user_id, (user_agent or "")[:300])
    box.user = user_id
    return token


async def create(ip: str = "?", user_agent: str = "") -> tuple[str, str, Sandbox]:
    """Личная копия демо для посетителя: (cookie демо, cookie панели, песочница). Может бросить Busy, NotReady, RateLimited."""
    from . import demo_data
    s = get_settings()
    await reap()
    if not _state["template"]:
        raise NotReady("заготовка ещё собирается")
    if len(_boxes) >= s.demo_max_sessions:
        raise Busy(_minutes_until_free())
    if not _rate_ok(ip):
        raise RateLimited("слишком много новых сеансов с этого адреса")
    name = "demo_s_" + secrets.token_hex(6)
    sid = secrets.token_urlsafe(32)
    async with _clone_lock:
        if not _state["template"]:
            raise NotReady("заготовка ещё собирается")
        await _admin().execute(f'CREATE DATABASE "{name}" TEMPLATE "{_state["template"]}"')
    pool = None
    try:
        pool = await _open_pool(name, 2)
        await demo_data.fresh_today(pool)
        now = time.time()
        box = Sandbox(sid_hash=_h(sid), db_name=name, pool=pool, created=now, last=now)
        session = await make_session(box, 1, user_agent)
    except Exception:
        if pool is not None:
            await pool.close()
        await _drop_db(name)
        raise
    _boxes[box.sid_hash] = box
    log.info("Демо: новый сеанс %s (всего %d)", name, len(_boxes))
    return sid, session, box


def _h(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()


def lookup(sid: str | None) -> Sandbox | None:
    if not sid:
        return None
    box = _boxes.get(_h(sid))
    if box and expired(box):
        return None
    return box


def expired(box: Sandbox, now: float | None = None) -> bool:
    s = get_settings()
    now = now or time.time()
    return now - box.last > s.demo_idle_min * 60 or now - box.created > s.demo_max_min * 60


async def drop(box: Sandbox) -> None:
    _boxes.pop(box.sid_hash, None)
    try:
        await asyncio.wait_for(box.pool.close(), 10)
    except Exception:
        pass
    await _drop_db(box.db_name)


async def reap() -> int:
    """Удаляет базы тех, кто ушёл или засиделся."""
    dead = [b for b in list(_boxes.values()) if expired(b)]
    for b in dead:
        await drop(b)
        log.info("Демо: сеанс %s закрыт (удалены его изменения)", b.db_name)
    return len(dead)


async def startup() -> None:
    """При запуске ядра: убрать базы, оставшиеся от прошлого запуска, и собрать заготовку."""
    rows = await _admin().fetch("SELECT datname FROM pg_database WHERE datname LIKE 'demo\\_s\\_%' OR datname LIKE 'demo\\_t\\_%'")
    for r in rows:
        await _drop_db(r["datname"])
    try:
        await build_template()
    except Exception:
        log.exception("Заготовка демо не собралась, повтор через 5 минут")
        _state["next_try"] = time.time() + 300


async def loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await reap()
            if (_state["template"] is None or _state["day"] != today()) and time.time() >= _state["next_try"]:
                try:
                    await build_template()
                except Exception:
                    log.exception("Заготовка демо не собралась, повтор через 5 минут")
                    _state["next_try"] = time.time() + 300
        except Exception:
            log.exception("Демо: фоновая задача")
        try:
            await asyncio.wait_for(stop.wait(), 30)
        except asyncio.TimeoutError:
            pass


async def shutdown() -> None:
    for b in list(_boxes.values()):
        await drop(b)


def status(box: Sandbox) -> dict:
    s = get_settings()
    now = time.time()
    left = min(box.created + s.demo_max_min * 60, box.last + s.demo_idle_min * 60) - now
    return {"seconds_left": max(0, int(left)), "idle_left": max(0, int(box.last + s.demo_idle_min * 60 - now)),
            "user": box.user, "ai_left": max(0, s.demo_ai_messages - box.ai_msgs),
            "ai_total": s.demo_ai_messages, "site": s.demo_site_url}


# ---------- ИИ: лимиты ----------

def _ai_day_ok() -> bool:
    d = today()
    if _ai_day[0] != d:
        _ai_day[0], _ai_day[1] = d, 0
    return _ai_day[1] < get_settings().demo_ai_daily


def ai_guard(has_image: bool = False, kind: str = "text") -> None:
    """Вызывается перед каждым обращением к ИИ (ai.GUARD). Бросает ai.AIError с понятным текстом."""
    from .ai import AIError
    s = get_settings()
    box = _current.get()
    if box is None:
        raise AIError(MSG_OFF)
    if has_image or kind != "text":
        raise AIError(MSG_FILES)
    if not s.llm_api_key.strip():
        raise AIError(MSG_OFF)
    if box.ai_calls >= s.demo_ai_calls:
        raise AIError(MSG_LIMIT.format(n=s.demo_ai_messages))
    if not _ai_day_ok():
        raise AIError(MSG_DAY)
    box.ai_calls += 1
    _ai_day[1] += 1


def chat_precheck(text: str, has_file: bool) -> dict | None:
    """Проверка сообщения из чата до разбора: ответ-заглушка или None (можно разбирать)."""
    s = get_settings()
    box = _current.get()

    def reply(t):
        return {"text": t, "buttons": [], "new_message": True}

    if has_file:
        return reply(MSG_FILES)
    t = (text or "").strip()
    if len(t) > MAX_TEXT:
        return reply(f"🧪 В демо сообщение — до {MAX_TEXT} символов. Сократите, пожалуйста.")
    low = t.lower()
    if low.startswith("/"):
        if any(low == c or low.startswith(c + " ") or low.startswith(c + "@") for c in BLOCKED_CMDS):
            return reply("🧪 Эта команда в демо недоступна: она настраивает Telegram, обновления и вход. "
                         "Остальные команды (/today, /month, /tasks, /habits) работают.")
        return None
    if box is None:
        return reply(MSG_OFF)
    if not s.llm_api_key.strip():
        return reply(MSG_OFF)
    if box.ai_msgs >= s.demo_ai_messages:
        return reply(MSG_LIMIT.format(n=s.demo_ai_messages))
    if not _ai_day_ok():
        return reply(MSG_DAY)
    box.ai_msgs += 1
    return None


# ---------- ASGI: база посетителя на каждый запрос ----------

def _cookies(headers: dict) -> dict:
    c = SimpleCookie()
    try:
        c.load(headers.get("cookie", ""))
    except Exception:
        return {}
    return {k: m.value for k, m in c.items()}


def _is_api(path: str) -> bool:
    return path.startswith("/web/api/")


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def path_allowed(path: str) -> bool:
    return path in ALLOWED_EXACT or any(_under(path, p) for p in ALLOWED_PREFIX)


def denied(path: str) -> bool:
    return path.startswith(DENIED_PREFIX)     # префиксы заданы с точной границей: «…/integrations/», «/web/login» и т. д.


# страницы и файлы, которым база посетителя не нужна
_NO_BOX_OK = ("/web/static/", "/web/manifest.webmanifest", "/web/sw.js")


async def _respond(send, status: int, body: bytes, ctype: str, extra: list | None = None) -> None:
    headers = [(b"content-type", ctype.encode()), (b"content-length", str(len(body)).encode()),
               (b"cache-control", b"no-store"), (b"x-robots-tag", b"noindex")] + (extra or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _json(send, status: int, obj: dict) -> None:
    await _respond(send, status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")


async def _redirect(send, to: str) -> None:
    await _respond(send, 303, b"", "text/plain", [(b"location", to.encode())])


class DemoMiddleware:
    """Выбирает базу посетителя по cookie demo_sid, ограничивает пути и частоту запросов. Включается только при DEMO_MODE."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not on():
            return await self.app(scope, receive, send)
        path = scope["path"]
        if not path_allowed(path):
            return await _json(send, 404, {"detail": "Нет такого адреса"})
        if denied(path):
            return await _json(send, 403, {"detail": "В демо это отключено"})
        if _under(path, "/web") and not path.startswith(_NO_BOX_OK):
            headers = {k.decode().lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            box = lookup(_cookies(headers).get(COOKIE))
            if box is None:
                if _is_api(path):
                    return await _json(send, 401, {"detail": "Демо-сеанс закончился"})
                return await _redirect(send, "/?e=expired")
            now = time.time()
            box.hits[:] = [t for t in box.hits if now - t < 10]
            if len(box.hits) > 150:
                return await _json(send, 429, {"detail": "Слишком много запросов, подождите немного"})
            box.hits.append(now)
            box.last = now
            tok_p, tok_b = db._override.set(box.pool), _current.set(box)
            try:
                return await self.app(scope, receive, send)
            finally:
                db._override.reset(tok_p)
                _current.reset(tok_b)
        return await self.app(scope, receive, send)


# ---------- страницы и маршруты ----------

def install() -> None:
    """Отключает всё, что в демо не нужно: Telegram, баланс, обновления, быстрое обновление курсов."""
    from . import ai, balance, market, notify, release
    ai.GUARD = ai_guard
    notify.SENDERS.clear()
    balance.enabled = lambda: False

    async def _not_owner(user) -> bool:
        return False

    release.is_owner = _not_owner
    market.TTL = {k: 30 * 3600 for k in market.TTL}


PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="robots" content="noindex">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Демо семейного ассистента</title>
<style>
:root{{--bg:#f4f6fa;--card:#fff;--ink:#16203a;--muted:#5b6475;--acc:#33519a;--cu:#a85a2c;--line:#dde3ee}}
@media(prefers-color-scheme:dark){{:root{{--bg:#121a2b;--card:#1b2540;--ink:#eaf0fb;--muted:#a3b0c9;--acc:#7da2ff;--cu:#e0925d;--line:#2b3858}}}}
body{{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink);margin:0;
min-height:100vh;display:flex;align-items:center;justify-content:center;padding:18px;box-sizing:border-box}}
.c{{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:30px 28px;max-width:520px;box-shadow:0 8px 32px #0002}}
h1{{margin:0 0 6px;font-size:26px}}h1 b{{color:var(--acc)}}.lead{{color:var(--muted);margin:0 0 18px;line-height:1.5}}
ul{{margin:0 0 20px;padding-left:20px;line-height:1.6}}li b{{color:var(--cu)}}
button{{font:inherit;background:var(--acc);color:#fff;border:0;border-radius:12px;padding:14px 30px;cursor:pointer;font-size:18px;width:100%}}
.note{{color:var(--muted);font-size:13.5px;margin-top:14px;line-height:1.5}}.err{{background:#a85a2c22;border:1px solid var(--cu);
border-radius:10px;padding:10px 14px;margin-bottom:16px}}a{{color:var(--acc)}}
</style></head><body><div class="c"><h1>🏠 Семейный <b>ассистент</b>: демо</h1>
<p class="lead">Живой кабинет с вымышленной семьёй: траты, питание, дела, гараж, поездки, лекарства. Нажмите и пробуйте.</p>{err}
<ul><li>Данные <b>вымышленные</b>, история за полгода.</li>
<li>Меняйте что угодно: ваши правки видны только вам.</li>
<li>Через <b>{idle} минут</b> без действий (или через час) всё сбрасывается.</li>
<li>Внизу работает чат: напишите «Пятёрочка 1450 продукты» или «съел борщ» (до {msgs} сообщений).</li></ul>
<form method="post" action="/demo/start" onsubmit="try{{localStorage.removeItem('chat-history-v1')}}catch(e){{}}"><button type="submit">Открыть демо</button></form>
<p class="note">Фото, голос и загрузка выписок в демо отключены. <a href="{site}">← На сайт проекта</a></p></div></body></html>"""

ERRORS = {
    "expired": "Демо-сеанс закончился, все правки сброшены. Нажмите кнопку, чтобы начать заново.",
    "busy": "Сейчас все места в демо заняты. Попробуйте через {m} мин.",
    "rate": "С вашего адреса слишком часто открывали демо. Попробуйте через час.",
    "notready": "Демо готовится (это бывает после обновления). Попробуйте через минуту.",
    "fail": "Не получилось открыть демо. Попробуйте ещё раз через минуту.",
}


def landing_html(error: str = "", minutes: int = 0) -> str:
    s = get_settings()
    msg = ERRORS.get(error, "").format(m=minutes)
    return PAGE.format(err=f'<div class="err">{esc(msg)}</div>' if msg else "", idle=s.demo_idle_min,
                       msgs=s.demo_ai_messages, site=esc(s.demo_site_url))


def inject_flag(html: str) -> str:
    """Помечает страницу панели или чата как демо (скрипт читает window.DEMO)."""
    return html.replace("<head>", "<head><script>window.DEMO=true;document.documentElement.classList.add('demo')</script>", 1)


# ---------- маршруты демо ----------
router = APIRouter()
_HDR = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}


def _headers(request) -> dict:
    return {k.lower(): v for k, v in request.headers.items()}


def _set_cookies(resp, sid: str, session: str, secure: bool) -> None:
    age = get_settings().demo_max_min * 60 + 300
    resp.set_cookie(COOKIE, sid, max_age=age, httponly=True, secure=secure, samesite="lax", path="/")
    resp.set_cookie(SESSION_COOKIE, session, max_age=age, httponly=True, secure=secure, samesite="lax", path="/web")


def _from_panel(request) -> bool:
    return _headers(request).get("x-requested-with") == "panel"


async def _open(request) -> RedirectResponse:
    """Создаёт новую песочницу и отправляет в панель; при отказе — на стартовую страницу с пояснением."""
    h = _headers(request)
    try:
        sid, session, _box = await create(_client_ip(h), h.get("user-agent", ""))
    except Busy as e:
        return RedirectResponse(f"/?e=busy&m={e.minutes}", status_code=303)
    except RateLimited:
        return RedirectResponse("/?e=rate", status_code=303)
    except NotReady:
        return RedirectResponse("/?e=notready", status_code=303)
    except Exception:
        log.exception("Демо: не удалось создать сеанс")
        return RedirectResponse("/?e=fail", status_code=303)
    resp = RedirectResponse("/web/", status_code=303)
    _set_cookies(resp, sid, session, h.get("x-forwarded-proto") == "https")
    return resp


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def landing(e: str = "", m: int = 0):
    return HTMLResponse(landing_html(e, m), headers=_HDR)


@router.get("/robots.txt", include_in_schema=False)
async def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /\n", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/demo", response_class=HTMLResponse, include_in_schema=False)
async def landing_alias(e: str = "", m: int = 0):
    return HTMLResponse(landing_html(e, m), headers=_HDR)


@router.post("/demo/start", include_in_schema=False)
async def start(request: Request):
    if lookup(_cookies(_headers(request)).get(COOKIE)):
        return RedirectResponse("/web/", status_code=303)       # уже открыто: вернуться в свой сеанс
    return await _open(request)


@router.post("/demo/reset", include_in_schema=False)
async def reset(request: Request):
    if not _from_panel(request):
        return JSONResponse({"detail": "Запрос не из панели"}, status_code=403)
    box = lookup(_cookies(_headers(request)).get(COOKIE))
    if box:
        await drop(box)
    return await _open(request)


@router.post("/demo/as", include_in_schema=False)
async def switch_user(request: Request, body: dict):
    """«Войти как …»: тот же сеанс, но от имени другого члена семьи (видно, чем отличаются личные и общие разделы)."""
    if not _from_panel(request):
        return JSONResponse({"detail": "Запрос не из панели"}, status_code=403)
    h = _headers(request)
    box = lookup(_cookies(h).get(COOKIE))
    if not box:
        return JSONResponse({"detail": "Демо-сеанс закончился"}, status_code=401)
    try:
        session = await make_session(box, int(body.get("user")), h.get("user-agent", ""))
    except (TypeError, ValueError):
        return JSONResponse({"detail": "Нет такого человека"}, status_code=400)
    resp = JSONResponse({"ok": True, "user": box.user})
    resp.set_cookie(SESSION_COOKIE, session, max_age=get_settings().demo_max_min * 60 + 300, httponly=True,
                    secure=h.get("x-forwarded-proto") == "https", samesite="lax", path="/web")
    return resp


@router.get("/demo/api/status", include_in_schema=False)
async def demo_status(request: Request):
    box = lookup(_cookies(_headers(request)).get(COOKIE))
    if not box:
        return JSONResponse({"detail": "Демо-сеанс закончился"}, status_code=410, headers=_HDR)
    return JSONResponse(status(box), headers=_HDR)
