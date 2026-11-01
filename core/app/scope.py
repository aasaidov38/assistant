"""Личные и общие разделы (версия 1.7).

У каждого раздела семьи есть режим:
  shared   — общий: все члены семьи видят и ведут одни и те же записи (траты, покупки, гараж…);
  personal — личный: каждый видит только свои записи (калории, лекарства, заметки…).
Режим меняет только владелец (первый в ALLOWED_USERS) — в панели «⚙️ Настройки» или командой /settings.

Как это работает в коде. При входе запроса (бот, Алиса, панель) вызывается enter(user): он запоминает,
кто смотрит, и режимы разделов семьи. Функции, которые читают списки, добавляют к SQL условие cond(...):
в общем разделе это «true», в личном — «<столбец автора> = <id того, кто смотрит>».
Фоновые задачи (напоминания) enter не вызывают и видят всё — получателя они выбирают сами (notify.remind).
"""
import contextvars
from dataclasses import dataclass, field

from . import db

SECTIONS = {
    #  раздел        название в настройках                         режим по умолчанию
    "finance":   ("💰 Траты, доходы и лимиты",                    "shared"),
    "shopping":  ("🛒 Список покупок",                             "shared"),
    "savings":   ("🎯 Накопления",                                 "shared"),
    "bills":     ("💳 Регулярные платежи",                         "shared"),
    "meters":    ("🔢 Счётчики",                                   "shared"),
    "papers":    ("📄 Документы, полисы, гарантии",                "shared"),
    "garage":    ("🏍 Гараж",                                      "shared"),
    "trips":     ("🧳 Поездки",                                    "shared"),
    "nutrition": ("🍽 Питание и калории",                          "personal"),
    "habits":    ("📈 Привычки: вода, шаги, вес, сон",             "personal"),
    "meds":      ("💊 Лекарства",                                  "personal"),
    "notes":     ("🗒 Заметки",                                    "personal"),
    "debts":     ("🤝 Долги",                                      "personal"),
}
DEFAULTS = {k: v[1] for k, v in SECTIONS.items()}


@dataclass
class _Viewer:
    user_id: int
    family_id: int
    modes: dict = field(default_factory=dict)


_viewer: contextvars.ContextVar[_Viewer | None] = contextvars.ContextVar("viewer", default=None)


async def modes(family_id: int) -> dict[str, str]:
    raw = await db.get_pool().fetchval("SELECT sections FROM families WHERE id=$1", family_id) or {}
    if isinstance(raw, str):
        import json
        raw = json.loads(raw or "{}")
    return {k: raw.get(k) if raw.get(k) in ("shared", "personal") else d for k, d in DEFAULTS.items()}


async def personal(family_id: int, section: str) -> bool:
    return (await modes(family_id)).get(section) == "personal"


async def enter(user) -> None:
    """Запоминает, кто смотрит. Вызывается в начале обработки сообщения, кнопки или запроса панели."""
    _viewer.set(_Viewer(user.id, user.family_id, await modes(user.family_id)))


def leave() -> None:
    _viewer.set(None)


class as_user:
    """async with scope.as_user(user): ... — смотреть глазами человека внутри фоновой задачи
    (утренняя сводка, недельный разбор), а после блока вернуть как было."""
    def __init__(self, user):
        self.user, self.token = user, None

    async def __aenter__(self):
        self.token = _viewer.set(_Viewer(self.user.id, self.user.family_id, await modes(self.user.family_id)))
        return self

    async def __aexit__(self, *exc):
        _viewer.reset(self.token)


def viewer_id() -> int | None:
    v = _viewer.get()
    return v.user_id if v else None


def is_personal(section: str) -> bool:
    """Личный ли раздел для того, кто сейчас смотрит (без enter — False)."""
    v = _viewer.get()
    return bool(v and v.modes.get(section) == "personal")


def only(section: str) -> int | None:
    """id того, чьи записи показывать, или None — показывать все записи семьи."""
    v = _viewer.get()
    return v.user_id if v and v.modes.get(section) == "personal" else None


def cond(section: str, column: str) -> str:
    """SQL-условие для списка: 'true' или '<column> = <id>'. id — целое число, подстановка безопасна."""
    uid = only(section)
    return "true" if uid is None else f"{column} = {int(uid)}"


def can_see(section: str | None, owner_id: int | None) -> bool:
    if not section:
        return True
    uid = only(section)
    return uid is None or owner_id == uid


async def is_owner(user) -> bool:
    first = await db.get_pool().fetchval("SELECT min(id) FROM users WHERE family_id=$1", user.family_id)
    return first == user.id


async def set_modes(user, changes: dict[str, str]) -> dict[str, str]:
    """Меняет режимы разделов (только владелец). Возвращает новые режимы."""
    if not await is_owner(user):
        raise PermissionError("Режим разделов меняет только владелец")
    cur = await modes(user.family_id)
    for k, v in (changes or {}).items():
        if k in SECTIONS and v in ("shared", "personal"):
            cur[k] = v
    await db.get_pool().execute("UPDATE families SET sections=$2::jsonb WHERE id=$1", user.family_id, cur)
    if _viewer.get() and _viewer.get().family_id == user.family_id:
        _viewer.get().modes = cur
    return cur


def mode_word(section: str) -> str:
    return "личный" if is_personal(section) else "общий"
