"""Канал «Яндекс Алиса»: приватный навык в Яндекс Диалогах.

Адрес вебхука для навыка: https://<PUBLIC_URL>/alice/<ALICE_SECRET>
Привязка: в Telegram команда /alice → бот даёт код → в навыке сказать «код 123456».

Алиса ждёт ответ не дольше ~3 секунд. Простые команды отвечаются сразу, а если ИИ думает дольше,
Алиса говорит «Принято», а полный ответ приходит в Telegram.
"""
import asyncio
import html
import logging
import re
import secrets

from fastapi import APIRouter, HTTPException, Request

from . import brain, db, notify, users
from .config import get_settings
from .schemas import Reply
from .util import esc

log = logging.getLogger("alice")
router = APIRouter()
WAIT_SECONDS = 2.4
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍⃣■-◿]+")
_EXIT = {"хватит", "выход", "стоп", "закончить", "всё", "все", "спасибо"}


def to_speech(text: str) -> str:
    """HTML-ответ ядра → простой текст для Алисы."""
    t = re.sub(r"<[^>]+>", "", text)
    t = html.unescape(t)
    t = _EMOJI.sub("", t)
    t = re.sub(r"[▰▱]+\s*\d*%?", "", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = "\n".join(line.strip(" ·•-") for line in t.splitlines() if line.strip(" ·•-"))
    return t[:1000]


def _answer(text: str, end: bool = False) -> dict:
    text = text[:1000] or "Готово."
    tts = text.replace("₽", "рублей").replace("ккал", "килокалорий").replace("№", "номер")
    return {"response": {"text": text, "tts": tts[:1000], "end_session": end}, "version": "1.0"}


async def link_code(user: users.User) -> Reply:
    s = get_settings()
    if not s.alice_secret or not s.public_url:
        return Reply(text="🎙 Алиса ещё не настроена: нужны PUBLIC_URL и ALICE_SECRET в .env (docs/INSTALL.md, раздел «Алиса»).")
    code = f"{secrets.randbelow(900000) + 100000}"
    await db.get_pool().execute(
        "INSERT INTO link_codes (code, user_id, channel, expires_at) VALUES ($1,$2,'alice',now()+interval '15 minutes')"
        " ON CONFLICT (code) DO UPDATE SET user_id=$2, expires_at=now()+interval '15 minutes'", code, user.id)
    return Reply(text=f"🎙 <b>Привязка Алисы</b>\n\nСкажите Алисе:\n«Алиса, запусти навык <i>Семейный помощник</i>»,"
                      f" а затем: «код <b>{code}</b>».\n\nКод действует 15 минут.")


async def _link(ext_id: str, code: str) -> str:
    async with db.get_pool().acquire() as conn, conn.transaction():
        uid = await conn.fetchval(
            "DELETE FROM link_codes WHERE code=$1 AND channel='alice' AND expires_at > now() RETURNING user_id", code)
        if not uid:
            return "Код не подошёл или устарел. Отправьте боту в Телеграм команду слэш алиса и назовите новый код."
        await conn.execute(
            "INSERT INTO user_identities (channel, external_id, user_id) VALUES ('alice', $1, $2)"
            " ON CONFLICT (channel, external_id) DO UPDATE SET user_id=$2", ext_id, uid)
    user = await users.get(uid)
    await notify.send(uid, Reply(text="🎙 Алиса привязана. Теперь можно говорить: «запиши трату 500 на такси», "
                                      "«выпил стакан воды», «что у нас на сегодня»."))
    return f"Готово, {user.name}! Теперь я вас узнаю. Скажите, например: запиши трату пятьсот рублей на такси."


async def _finish_in_telegram(user: users.User, phrase: str, task: asyncio.Task) -> None:
    try:
        reply = await task
    except Exception:
        log.exception("Ошибка обработки фразы из Алисы")
        reply = Reply(text="⚠️ Не получилось обработать фразу из Алисы.")
    from . import prefs
    if (await prefs.get(user.id))["mirror"]:
        await notify.send(user.id, Reply(text=f"🎙 <i>Алиса: «{esc(phrase)}»</i>\n{reply.text}", buttons=reply.buttons),
                          quiet="drop")


_background: set[asyncio.Task] = set()


@router.post("/alice/{secret}")
async def webhook(secret: str, request: Request):
    s = get_settings()
    if not s.alice_secret or not secrets.compare_digest(secret, s.alice_secret):
        raise HTTPException(404)
    body = await request.json()
    sess = body.get("session") or {}
    ext_id = ((sess.get("user") or {}).get("user_id") or (sess.get("application") or {}).get("application_id")
              or sess.get("user_id") or "")
    req = body.get("request") or {}
    phrase = (req.get("original_utterance") or req.get("command") or "").strip()
    cmd = (req.get("command") or phrase).lower().strip()
    if not ext_id:
        return _answer("Не получилось вас узнать. Войдите в аккаунт Яндекса и попробуйте снова.", end=True)

    user = await users.resolve("alice", ext_id)
    m = re.search(r"\b(\d[\d ]{4,8}\d)\b", cmd)
    if m and ("код" in cmd or not user):
        return _answer(await _link(ext_id, m.group(1).replace(" ", "")))
    if not user:
        return _answer("Здравствуйте! Чтобы я вас узнавала, отправьте боту в Телеграм команду слэш алиса "
                       "и назовите мне код из ответа. Например: код один два три четыре пять шесть.")
    if not cmd or sess.get("new") and cmd in ("", "помощь", "что ты умеешь"):
        return _answer(f"Привет, {user.name}! Скажите, что записать: трату, еду, воду, дело или покупку. "
                       "Или спросите: что у нас на сегодня.")
    if cmd in _EXIT:
        return _answer("До связи!", end=True)

    task = asyncio.create_task(brain.handle_text(user, phrase, source="alice"))
    # Полный ответ с кнопками (отменить, исправить) всегда дублируется в Telegram
    bg = asyncio.create_task(_finish_in_telegram(user, phrase, task))
    _background.add(bg)
    bg.add_done_callback(_background.discard)
    try:
        reply = await asyncio.wait_for(asyncio.shield(task), timeout=WAIT_SECONDS)
    except asyncio.TimeoutError:
        return _answer("Принято! Ответ пришлю в Телеграм.")
    except Exception:
        log.exception("Ошибка обработки фразы из Алисы")
        return _answer("Что-то пошло не так. Попробуйте ещё раз.")
    text = reply.text
    if text.startswith("💾") or "\n\n📈" in text:
        text = text.split("\n\n")[0]       # для голоса хватит подтверждения, подробности в боте
    return _answer(to_speech(text))
