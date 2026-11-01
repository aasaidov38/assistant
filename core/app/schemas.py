"""Формат обмена между ядром и каналами (Telegram, Алиса, веб и др.)."""
from typing import Literal

from pydantic import BaseModel, Field


class Button(BaseModel):
    text: str
    action: str = ""     # строка до 64 байт, приходит обратно в /v1/action
    url: str | None = None   # если задано — кнопка-ссылка (открывает адрес, в ядро ничего не приходит)


class Reply(BaseModel):
    text: str
    buttons: list[list[Button]] = Field(default_factory=list)
    new_message: bool = False      # для кнопок: прислать отдельным сообщением, а не заменять исходное


class Inbound(BaseModel):
    """Сообщение от пользователя из любого канала."""
    channel: str = "telegram"
    external_user_id: str
    kind: Literal["text", "photo", "voice", "document"] = "text"
    text: str = ""                 # текст или подпись к фото
    file_b64: str | None = None    # фото или голос в base64
    mime: str | None = None
    filename: str | None = None    # для документов (выписка банка и т.п.)


class Action(BaseModel):
    """Нажатие кнопки."""
    channel: str = "telegram"
    external_user_id: str
    action: str
