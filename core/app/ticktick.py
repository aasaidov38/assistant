"""Интеграция с TickTick (Open API, OAuth2). Документация: https://developer.ticktick.com/"""
import hashlib
import hmac
import logging
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import get_settings
from .db import get_pool

log = logging.getLogger("ticktick")
AUTH_URL = "https://ticktick.com/oauth/authorize"
TOKEN_URL = "https://ticktick.com/oauth/token"
API = "https://api.ticktick.com/open/v1"
SCOPE = "tasks:read tasks:write"
_client = httpx.AsyncClient(timeout=30)


class TickTickError(Exception):
    pass


def configured() -> bool:
    s = get_settings()
    return bool(s.ticktick_client_id and s.ticktick_client_secret)


def _sign(user_id: int) -> str:
    key = get_settings().core_api_key.encode()
    return hmac.new(key, f"tt:{user_id}".encode(), hashlib.sha256).hexdigest()[:20]


def make_state(user_id: int) -> str:
    return f"{user_id}.{_sign(user_id)}"


def check_state(state: str) -> int | None:
    uid, _, sig = (state or "").partition(".")
    if uid.isdigit() and hmac.compare_digest(sig, _sign(int(uid))):
        return int(uid)
    return None


def auth_link(user_id: int) -> str:
    s = get_settings()
    return AUTH_URL + "?" + urlencode({
        "scope": SCOPE, "client_id": s.ticktick_client_id, "state": make_state(user_id),
        "redirect_uri": s.ticktick_redirect_uri, "response_type": "code"})


def parse_pasted_url(text: str) -> tuple[str, str] | None:
    """Пользователь прислал адрес из браузера после входа в TickTick: достаём code и state."""
    if "code=" not in text or "state=" not in text:
        return None
    q = parse_qs(urlparse(text.strip()).query)
    if "code" in q and "state" in q:
        return q["code"][0], q["state"][0]
    return None


async def exchange_code(code: str, state: str) -> int:
    """Меняет code на токен и сохраняет его. Возвращает id пользователя."""
    s = get_settings()
    user_id = check_state(state)
    if user_id is None:
        raise TickTickError("Ссылка устарела или повреждена. Запросите новую командой /ticktick")
    r = await _client.post(TOKEN_URL, auth=(s.ticktick_client_id, s.ticktick_client_secret), data={
        "code": code, "grant_type": "authorization_code", "scope": SCOPE,
        "redirect_uri": s.ticktick_redirect_uri})
    if r.status_code != 200:
        raise TickTickError(f"TickTick не выдал токен ({r.status_code}): {r.text[:200]}")
    tok = r.json()
    data = {"access_token": tok["access_token"],
            "expires_at": int(time.time()) + int(tok.get("expires_in") or 180 * 86400),
            "last_sync": None, "expired_notified": False}
    await get_pool().execute(
        "INSERT INTO integrations (user_id, provider, data) VALUES ($1, 'ticktick', $2)"
        " ON CONFLICT (user_id, provider) DO UPDATE SET data=EXCLUDED.data, updated_at=now()",
        user_id, data)
    return user_id


async def _token(user_id: int) -> str | None:
    if not configured():
        return None
    row = await get_pool().fetchrow(
        "SELECT data FROM integrations WHERE user_id=$1 AND provider='ticktick'", user_id)
    if not row:
        return None
    d = row["data"]
    if d.get("expires_at", 0) < time.time() + 60:
        return None
    return d["access_token"]


async def is_connected(user_id: int) -> bool:
    return await _token(user_id) is not None


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")


async def create_task(user_id: int, title: str, notes: str | None, due_at: datetime | None,
                      all_day: bool, remind_minutes: int | None) -> dict | None:
    """Создаёт задачу во «Входящих» TickTick. None, если TickTick не подключён."""
    tok = await _token(user_id)
    if not tok:
        return None
    body: dict = {"title": title, "timeZone": get_settings().tz}
    if notes:
        body["content"] = notes
    if due_at:
        body.update({"startDate": _fmt(due_at), "dueDate": _fmt(due_at), "isAllDay": all_day})
        if remind_minutes is not None and not all_day:
            body["reminders"] = [f"TRIGGER:-PT{int(remind_minutes)}M" if remind_minutes else "TRIGGER:PT0S"]
    r = await _client.post(f"{API}/task", headers={"Authorization": f"Bearer {tok}"}, json=body)
    if r.status_code != 200:
        raise TickTickError(f"TickTick {r.status_code}: {r.text[:200]}")
    return r.json()


async def _call(user_id: int, method: str, path: str, **kw) -> httpx.Response | None:
    tok = await _token(user_id)
    if not tok:
        return None
    r = await _client.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {tok}"}, **kw)
    if r.status_code == 401:
        raise TickTickError("TickTick отклонил доступ (401). Подключите заново: /ticktick")
    return r


async def complete_task(user_id: int, project_id: str, task_id: str) -> None:
    r = await _call(user_id, "POST", f"/project/{project_id}/task/{task_id}/complete")
    if r is not None and r.status_code not in (200, 204, 404):
        raise TickTickError(f"TickTick {r.status_code}: {r.text[:200]}")


async def delete_task(user_id: int, project_id: str, task_id: str) -> None:
    r = await _call(user_id, "DELETE", f"/project/{project_id}/task/{task_id}")
    if r is not None and r.status_code not in (200, 204, 404):
        raise TickTickError(f"TickTick {r.status_code}: {r.text[:200]}")


async def projects(user_id: int) -> list[dict]:
    r = await _call(user_id, "GET", "/project")
    if r is None:
        return []
    if r.status_code != 200:
        raise TickTickError(f"TickTick {r.status_code}: {r.text[:200]}")
    return r.json() or []


async def project_tasks(user_id: int, project_id: str) -> list[dict] | None:
    """Невыполненные задачи списка. None, если списка нет."""
    r = await _call(user_id, "GET", f"/project/{project_id}/data")
    if r is None or r.status_code == 404:
        return None
    if r.status_code != 200:
        raise TickTickError(f"TickTick {r.status_code}: {r.text[:200]}")
    return (r.json() or {}).get("tasks") or []


async def get_task(user_id: int, project_id: str, task_id: str) -> dict | None:
    """Задача по id. None — удалена. Исключение — TickTick недоступен (тогда ничего не трогаем)."""
    r = await _call(user_id, "GET", f"/project/{project_id}/task/{task_id}")
    if r is None:
        raise TickTickError("не подключён")
    if r.status_code == 404 or (r.status_code == 200 and not r.content.strip()):
        return None
    if r.status_code != 200:
        raise TickTickError(f"TickTick {r.status_code}: {r.text[:200]}")
    return r.json()


async def token_info(user_id: int) -> dict:
    row = await get_pool().fetchrow("SELECT data, updated_at FROM integrations WHERE user_id=$1 AND provider='ticktick'",
                                    user_id)
    if not row:
        return {"connected": False}
    d = row["data"]
    return {"connected": d.get("expires_at", 0) > time.time() + 60, "expires_at": d.get("expires_at"),
            "last_sync": d.get("last_sync"), "last_error": d.get("last_error"),
            "notify": d.get("notify", True) is not False}


async def save_sync_state(user_id: int, **fields) -> None:
    await get_pool().execute(
        "UPDATE integrations SET data = data || $2::jsonb WHERE user_id=$1 AND provider='ticktick'", user_id, fields)
