"""Чат с ассистентом из веб-панели и Android-приложения: /web/chat.

Страница работает так же, как переписка с ботом в Telegram: текст, фото чеков и еды,
голосовые, файлы-выписки и кнопки под ответами. Сообщение передаётся в то же ядро
(brain.handle_inbound) от имени Telegram-аккаунта вошедшего пользователя, поэтому
всё записывается и отменяется точно так же, как из бота.

Вход — та же сессия веб-панели (cookie после ссылки /web из бота). Личные и общие
разделы (scope) учитываются так же, как в боте: brain.handle_inbound сам вызывает scope.enter.
Версия 1.7.1.
"""
import base64
import binascii
import logging
import typing
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from . import brain, users, web
from .schemas import Action, Inbound

log = logging.getLogger(__name__)
router = APIRouter()

CHAT_HTML = Path(__file__).resolve().parent / "static" / "chat.html"
MAX_FILE_BYTES = 15 * 1024 * 1024
# Вид сообщения «файл-документ» (выписка PDF/CSV) — как у адаптера Telegram
DOC_KIND = "document"
# Поле с именем файла в Inbound (по расширению bank.import_file понимает CSV или PDF)
NAME_FIELDS = ("filename",)


def _allowed_kinds() -> set[str]:
    ann = Inbound.model_fields["kind"].annotation
    return set(typing.get_args(ann)) or {"text", "photo", "voice"}


def _guard(request: Request) -> None:
    # Тот же заголовок, что у панели (web.current_user тоже его проверяет):
    # браузер не даст чужому сайту отправить такой запрос с нашей cookie
    if request.headers.get("x-requested-with") != "panel":
        raise HTTPException(403, "Запрос не из панели")


async def _telegram_id(user: users.User) -> str:
    for channel, ext in await users.identities(user.id):
        if channel == "telegram":
            return ext
    raise HTTPException(409, "У пользователя нет привязки к Telegram")


def _reply(r) -> dict:
    d = r.model_dump() if hasattr(r, "model_dump") else dict(r)
    return {"text": d.get("text", ""), "buttons": d.get("buttons", []),
            "new_message": bool(d.get("new_message", False))}


# ---------- страница ----------

@router.get("/web/chat")
async def chat_page(request: Request):
    if not await web._session_user(request):
        return RedirectResponse("/web/", status_code=303)   # там объяснение, как войти
    return FileResponse(CHAT_HTML, media_type="text/html",
                        headers={"Cache-Control": "no-cache"})


# ---------- API ----------

class SendIn(BaseModel):
    text: str = ""
    file_b64: str | None = None
    mime: str | None = None
    name: str | None = None
    voice: bool = False


@router.post("/web/api/chat/send")
async def chat_send(body: SendIn, request: Request, user: users.User = Depends(web.current_user)):
    _guard(request)
    payload: dict = {"channel": "telegram", "external_user_id": await _telegram_id(user),
                     "text": (body.text or "")[:4000]}

    if body.file_b64:
        try:
            raw = base64.b64decode(body.file_b64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(400, "Файл повреждён")
        if len(raw) > MAX_FILE_BYTES:
            raise HTTPException(413, "Файл больше 15 МБ")
        mime = (body.mime or "").lower() or "application/octet-stream"
        if body.voice or mime.startswith("audio/"):
            kind = "voice"
        elif mime.startswith("image/"):
            kind = "photo"
        else:
            kind = DOC_KIND
        if kind not in _allowed_kinds():
            return {"text": "📎 Такие файлы через приложение ядро пока не принимает. "
                            "Отправьте этот файл боту в Telegram.", "buttons": [], "new_message": True}
        payload.update(kind=kind, file_b64=body.file_b64, mime=mime)
        for f in NAME_FIELDS:
            if f in Inbound.model_fields and body.name:
                payload[f] = body.name[:200]
                break
    else:
        if not payload["text"].strip():
            raise HTTPException(400, "Пустое сообщение")
        payload["kind"] = "text"

    payload = {k: v for k, v in payload.items() if k in Inbound.model_fields}
    log.info("chat: user=%s kind=%s", user.id, payload.get("kind"))
    return _reply(await brain.handle_inbound(Inbound(**payload)))


class ActionIn(BaseModel):
    action: str


@router.post("/web/api/chat/action")
async def chat_action(body: ActionIn, request: Request, user: users.User = Depends(web.current_user)):
    _guard(request)
    act = Action(channel="telegram", external_user_id=await _telegram_id(user), action=body.action[:64])
    return _reply(await brain.handle_action(act))
