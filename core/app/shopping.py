"""Семейный список покупок: общий для всех членов семьи."""
import re

from . import db, scope, tasks
from .schemas import Button, Reply
from .users import User
from .util import esc, plural


async def add(user: User, items: list[dict]) -> Reply:
    added = []
    pool = db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        for it in items:
            title = str(it.get("title") or "").strip()[:200]
            if not title:
                continue
            exists = await conn.fetchval(
                "SELECT id FROM shopping_items WHERE family_id=$1 AND lower(title)=lower($2)"
                f" AND bought_at IS NULL AND deleted_at IS NULL AND {scope.cond('shopping', 'added_by')}", user.family_id, title)
            if exists:
                continue
            await conn.execute("INSERT INTO shopping_items (family_id, title, qty, added_by, dept) VALUES ($1,$2,$3,$4,$5)",
                               user.family_id, title, it.get("qty") or None, user.id,
                               (str(it.get("dept") or "").strip().lower()[:40] or None))
            added.append(title)
        if added:
            await db.emit(conn, "shopping.added", {"family_id": user.family_id, "items": added, "by": user.name})
    head = ("🛒 Добавил: " + ", ".join(esc(a) for a in added)) if added else "🛒 Это уже есть в списке."
    return await show(user, head)


async def open_items(family_id: int) -> list[dict]:
    rows = await db.get_pool().fetch(
        "SELECT s.id, s.title, s.qty, s.dept, u.name AS added_by FROM shopping_items s"
        " LEFT JOIN users u ON u.id=s.added_by"
        f" WHERE s.family_id=$1 AND s.bought_at IS NULL AND s.deleted_at IS NULL AND {scope.cond('shopping', 's.added_by')}"
        " ORDER BY coalesce(s.dept, 'яя'), s.id LIMIT 60", family_id)
    return [dict(r) for r in rows]


async def show(user: User, head: str = "") -> Reply:
    rows = await open_items(user.family_id)
    text = (head + "\n\n" if head else "")
    if not rows:
        return Reply(text=text + "🛒 Список покупок пуст.\nДобавить: <code>купить молоко и хлеб</code> "
                                 "или <code>закончился порошок</code>")
    text += f"🛒 <b>Список покупок</b> ({plural(len(rows), 'позиция', 'позиции', 'позиций')})\n"
    buttons = []
    dept = "-"
    for i, r in enumerate(rows, 1):
        if r["dept"] != dept and any(x["dept"] for x in rows):
            dept = r["dept"]
            text += f"\n<i>{esc((dept or 'другое').capitalize())}</i>\n"
        qty = f" ({esc(r['qty'])})" if r["qty"] else ""
        text += f"{i}. {esc(r['title'])}{qty}\n"
        if i <= 30:
            buttons.append(Button(text=f"✅ {r['title'][:20]}", action=f"s:{r['id']}:buy"))
    rows_b = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows_b.append([Button(text="📲 В TickTick", action="s:0:tt"),
                   Button(text="🧹 Очистить", action="s:0:clear")])
    text += "\nНажмите на товар, когда купите. Чек из магазина вычеркнет купленное сам."
    return Reply(text=text, buttons=rows_b)


async def handle_action(user: User, parts: list[str]) -> Reply:
    """s:<id>:buy | s:0:clear | s:0:tt"""
    pool = db.get_pool()
    op = parts[2]
    if op == "buy":
        await pool.execute("UPDATE shopping_items SET bought_at=now() WHERE id=$1 AND family_id=$2",
                           int(parts[1]), user.family_id)
        return await show(user)
    if op == "clear":
        n = await pool.fetchval(
            "WITH d AS (UPDATE shopping_items SET deleted_at=now() WHERE family_id=$1 AND bought_at IS NULL"
            f" AND deleted_at IS NULL AND {scope.cond('shopping', 'added_by')} RETURNING 1) SELECT count(*) FROM d",
            user.family_id)
        return Reply(text=f"🧹 Список очищен ({plural(n, 'позиция', 'позиции', 'позиций')}).")
    if op == "tt":
        rows = await open_items(user.family_id)
        if not rows:
            return Reply(text="🛒 Список покупок пуст.")
        notes = "\n".join(f"- {r['title']}" + (f" ({r['qty']})" if r["qty"] else "") for r in rows)
        task, note = await tasks.save(user, {"title": "Список покупок", "notes": notes}, source="shopping")
        return tasks.render_saved(task, note, header="🛒 Список покупок отправлен задачей")
    return Reply(text="Неизвестное действие.")


def _stems(text: str) -> set[str]:
    """Основы слов длиной от 4 букв: «молоко» → «моло». Грубо, но для сверки с чеком хватает."""
    words = re.findall(r"[а-яa-z]{4,}", text.lower().replace("ё", "е"))
    return {w[:4] if len(w) >= 5 else w[:3] for w in words}


async def cross_off_from_receipt(user: User, receipt_items: list[dict]) -> list[str]:
    """Вычёркивает из списка покупок то, что есть в чеке. Возвращает вычеркнутые названия."""
    rows = await open_items(user.family_id)
    if not rows or not receipt_items:
        return []
    bought_stems = set()
    for i in receipt_items:
        bought_stems |= _stems(str(i.get("name") or ""))
    done = []
    for r in rows:
        st = _stems(r["title"])
        if st and st & bought_stems:
            done.append(r)
    if done:
        await db.get_pool().execute("UPDATE shopping_items SET bought_at=now() WHERE id = ANY($1::bigint[])",
                                    [r["id"] for r in done])
    return [r["title"] for r in done]
