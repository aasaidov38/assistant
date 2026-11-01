"""Семейные заметки: размеры одежды, где лежат документы, какие фильтры у пылесоса…

«запомни: размер обуви у младшей 31» → сохраняется.
«какой размер обуви у младшей?» → ИИ отвечает только по заметкам семьи.
Пароли, номера карт и документов не сохраняются (защита и в инструкции для ИИ, и здесь в коде).
"""
import re

from . import ai, db, scope
from .prompts import NOTES_ANSWER
from .schemas import Button, Reply
from .users import User
from .util import esc

_SECRET = re.compile(r"парол|пин-?код|\bpin\b|cvv|cvc|(?:\d[ -]?){13,19}|серия\s*\d|номер паспорт|снилс", re.I)
REFUSE = ("🔒 Пароли, ПИН-коды, номера карт и документов я не сохраняю: если доступ к боту попадёт не в те руки, "
          "это будет утечка. Храните их в менеджере паролей.")


async def save(user: User, subject: str | None, text: str) -> Reply:
    text = (text or "").strip()[:1000]
    subject = (subject or "").strip()[:80] or None
    if not text:
        return Reply(text="Что запомнить? Пример: <code>запомни: размер обуви у младшей 31</code>")
    if _SECRET.search(text) or (subject and _SECRET.search(subject)):
        return Reply(text=REFUSE)
    nid = await db.get_pool().fetchval("INSERT INTO family_notes (family_id, user_id, subject, text) VALUES ($1,$2,$3,$4)"
                                       " RETURNING id", user.family_id, user.id, subject, text)
    return Reply(text=f"🗒 Запомнил{(' (' + esc(subject) + ')') if subject else ''}: {esc(text)}",
                 buttons=[[Button(text="↩️ Отменить", action=f"u:n.{nid}:del")]])


async def all_notes(family_id: int, limit: int = 300) -> list[dict]:
    rows = await db.get_pool().fetch(
        "SELECT n.id, n.subject, n.text, n.created_at, u.name AS user_name FROM family_notes n LEFT JOIN users u ON u.id=n.user_id"
        f" WHERE n.family_id=$1 AND n.deleted_at IS NULL AND {scope.cond('notes', 'n.user_id')}"
        " ORDER BY n.created_at DESC LIMIT $2", family_id, limit)
    return [dict(r) for r in rows]


async def ask(user: User, question: str, header: str = "") -> Reply:
    ns = await all_notes(user.family_id)
    pre = header + "\n" if header else ""
    if not ns:
        return Reply(text=pre + "🗒 В заметках пока пусто. Сохраните так: <code>запомни: размер обуви у младшей 31</code>")
    lines = "\n".join(f"[{n['created_at']:%d.%m.%Y}] {n['subject'] + ': ' if n['subject'] else ''}{n['text']}" for n in reversed(ns))
    raw = await ai.complete(NOTES_ANSWER, f"Заметки семьи:\n{lines}\n\nВопрос: {question}", max_tokens=500)
    return Reply(text=pre + "🗒 " + esc(raw.strip()))


async def list_reply(user: User, query: str = "") -> Reply:
    ns = await all_notes(user.family_id, 500)
    q = query.lower().replace("ё", "е").strip()
    if q:
        stems = [w[:max(3, len(w) - 2)] for w in q.split() if len(w) >= 3]
        ns = [n for n in ns if all(s in (f"{n['subject'] or ''} {n['text']}").lower().replace("ё", "е") for s in stems)]
    if not ns:
        return Reply(text="🗒 Ничего не нашёл." if q else "🗒 Заметок пока нет. Пример: <code>запомни: фильтр пылесоса Philips FC8010</code>")
    text = ("🗒 <b>Мои заметки</b>" if scope.is_personal("notes") else "🗒 <b>Заметки семьи</b>") + (f" по запросу «{esc(query)}»" if q else "") + "\n" + "\n".join(
        f"• {('<b>' + esc(n['subject']) + '</b>: ') if n['subject'] else ''}{esc(n['text'])}" for n in ns[:30])
    if len(ns) > 30:
        text += f"\n… всего {len(ns)}. Все — в панели, вкладка «🏠 Дом»."
    return Reply(text=text)
