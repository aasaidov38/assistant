"""Telegram-адаптер. Только принимает сообщения и показывает ответы.
Вся логика живёт в ядре (core), поэтому другие каналы (Алиса, веб, SMS) подключаются так же.
"""
import asyncio
import base64
import io
import logging
import os

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)
from aiogram.utils.chat_action import ChatActionSender

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("telegram")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CORE_URL = os.environ.get("CORE_URL", "http://core:8000")
API_KEY = os.environ["CORE_API_KEY"]

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True))
dp = Dispatcher()
core = httpx.AsyncClient(base_url=CORE_URL, headers={"X-API-Key": API_KEY},
                         timeout=httpx.Timeout(180.0, connect=10.0))

MENU = ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True, keyboard=[
    [KeyboardButton(text="📊 Сегодня"), KeyboardButton(text="💰 Месяц"), KeyboardButton(text="🎯 Бюджет")],
    [KeyboardButton(text="✅ Задачи"), KeyboardButton(text="🛒 Покупки"), KeyboardButton(text="🌐 Панель")],
])


def keyboard(reply: dict) -> InlineKeyboardMarkup | None:
    rows = reply.get("buttons") or []
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=b["text"], url=b["url"]) if b.get("url")
         else InlineKeyboardButton(text=b["text"], callback_data=b["action"]) for b in row] for row in rows])


async def ask_core(path: str, payload: dict) -> dict:
    try:
        r = await core.post(path, json=payload)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.exception("Ядро не ответило")
        return {"text": f"⚠️ Ядро ассистента недоступно, попробуйте через минуту.\n<code>{type(e).__name__}</code>"}


async def download_b64(file_id: str) -> str:
    buf = io.BytesIO()
    await bot.download(file_id, destination=buf)
    return base64.b64encode(buf.getvalue()).decode()


async def answer(message: Message, payload: dict) -> None:
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        reply = await ask_core("/v1/inbound", payload)
    await message.answer(reply["text"], reply_markup=keyboard(reply) or MENU)


def base(message: Message) -> dict:
    return {"channel": "telegram", "external_user_id": str(message.from_user.id)}


@dp.message(F.photo)
async def on_photo(message: Message):
    photo = message.photo[-1]   # самое большое разрешение
    await answer(message, {**base(message), "kind": "photo", "text": message.caption or "",
                           "file_b64": await download_b64(photo.file_id), "mime": "image/jpeg"})


@dp.message(F.document & F.document.mime_type.startswith("image/"))
async def on_image_file(message: Message):
    doc = message.document
    await answer(message, {**base(message), "kind": "photo", "text": message.caption or "",
                           "file_b64": await download_b64(doc.file_id), "mime": doc.mime_type})


@dp.message(F.document)
async def on_document(message: Message):
    """Файлы: банковская выписка (CSV, PDF). Остальное ядро вежливо отклонит."""
    doc = message.document
    if (doc.file_size or 0) > 15 * 1024 * 1024:
        await message.answer("Файл больше 15 МБ — слишком большой. Выгрузите выписку за период поменьше.", reply_markup=MENU)
        return
    await message.answer("🏦 Разбираю файл, это может занять до минуты…")
    await answer(message, {**base(message), "kind": "document", "text": message.caption or "",
                           "file_b64": await download_b64(doc.file_id), "mime": doc.mime_type or "",
                           "filename": doc.file_name or ""})


@dp.message(F.voice | F.audio)
async def on_voice(message: Message):
    media = message.voice or message.audio
    await answer(message, {**base(message), "kind": "voice",
                           "file_b64": await download_b64(media.file_id),
                           "mime": media.mime_type or "audio/ogg"})


@dp.message(F.text)
async def on_text(message: Message):
    await answer(message, {**base(message), "kind": "text", "text": message.text})


@dp.message()
async def on_other(message: Message):
    await message.answer("Пока понимаю текст, голосовые, фото и файлы выписок (CSV, PDF).", reply_markup=MENU)


@dp.callback_query()
async def on_button(cb: CallbackQuery):
    await cb.answer()
    reply = await ask_core("/v1/action", {"channel": "telegram", "external_user_id": str(cb.from_user.id),
                                          "action": cb.data or ""})
    if reply.get("new_message"):
        await cb.message.answer(reply["text"], reply_markup=keyboard(reply))
        return
    try:
        await cb.message.edit_text(reply["text"], reply_markup=keyboard(reply))
    except Exception:
        await cb.message.answer(reply["text"], reply_markup=keyboard(reply))


async def main():
    await bot.set_my_commands([
        BotCommand(command="today", description="Итоги дня: питание и траты"),
        BotCommand(command="month", description="Финансы за месяц"),
        BotCommand(command="tasks", description="Открытые задачи"),
        BotCommand(command="shop", description="Список покупок"),
        BotCommand(command="budget", description="Лимиты бюджета на месяц"),
        BotCommand(command="week", description="Разбор недели от ИИ"),
        BotCommand(command="bills", description="Регулярные платежи"),
        BotCommand(command="savings", description="Накопления"),
        BotCommand(command="meds", description="Лекарства на сегодня"),
        BotCommand(command="drug", description="Инструкция к лекарству: /drug название"),
        BotCommand(command="diet", description="Диетолог: разбор недели и вопросы про питание"),
        BotCommand(command="trip", description="Поездка: бюджет, сборы, погода"),
        BotCommand(command="garage", description="Гараж: пробег, ТО, страховки"),
        BotCommand(command="docs", description="Документы и гарантии"),
        BotCommand(command="meters", description="Счётчики"),
        BotCommand(command="notes", description="Заметки"),
        BotCommand(command="debts", description="Долги: кто кому должен"),
        BotCommand(command="cards", description="Карты лояльности: штрихкод на кассе"),
        BotCommand(command="app", description="Установить ассистента как приложение"),
        BotCommand(command="version", description="Версия, обновления, резервные копии"),
        BotCommand(command="settings", description="Настройки и разделы"),
        BotCommand(command="history", description="Последние записи: отменить или вернуть"),
        BotCommand(command="undo", description="Отменить последнюю запись"),
        BotCommand(command="goal", description="Цель по калориям, например /goal 1800"),
        BotCommand(command="ticktick", description="Подключить TickTick"),
        BotCommand(command="help", description="Справка"),
    ])
    log.info("Telegram-адаптер запущен")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
