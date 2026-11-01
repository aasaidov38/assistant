"""Вопросы к своим данным обычным языком: «сколько потратили на продукты в сентябре?».

Как это работает:
  1) ИИ пишет SQL-запрос к представлениям ro.* (только чтение, только своя семья);
  2) ядро выполняет его от роли assistant_ro в транзакции только для чтения, с ограничением 5 секунд;
  3) ИИ пересказывает результат человеческим языком.
"""
import json
import logging
import re

from . import ai, db, finance, users
from .config import get_settings
from .prompts import DATA_ANSWER, SQL_WRITER
from .schemas import Reply
from .util import WEEKDAYS, esc, now

log = logging.getLogger("analytics")

# Запрещённое в запросе ИИ: смена настроек, служебные функции, запись
_FORBIDDEN = re.compile(
    r"\b(set_config|current_setting|pg_\w+|dblink\w*|lo_\w+|copy|insert|update|delete|merge|drop|alter|create|"
    r"grant|revoke|truncate|vacuum|call|do|execute|prepare|listen|notify|lock|set|reset)\b|;|--|/\*",
    re.I)


class QueryError(Exception):
    pass


def check_sql(sql: str) -> str:
    sql = (sql or "").strip().rstrip(";").strip()
    if not re.match(r"(?is)^\s*(select|with)\b", sql):
        raise QueryError("запрос должен начинаться с SELECT или WITH")
    # Строковые литералы ('...') не проверяем: в них могут быть любые слова
    bare = re.sub(r"'(?:[^']|'')*'", "''", sql)
    m = _FORBIDDEN.search(bare)
    if m:
        raise QueryError(f"недопустимо в запросе: {m.group(0)}")
    # К каким таблицам можно обращаться, ограничивает сама роль assistant_ro: ей доступны только ro.*
    return sql


async def run_sql(family_id: int, sql: str, user_id: int | None = None) -> list[dict]:
    sql = check_sql(sql)
    async with db.get_pool().acquire() as conn:
        async with conn.transaction(readonly=True):
            await conn.execute(f"SET LOCAL TIME ZONE '{get_settings().tz}'")
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            await conn.execute("SELECT set_config('app.family_id', $1, true)", str(family_id))
            # кто спрашивает: в личных разделах видны только его записи (ro.can_see)
            await conn.execute("SELECT set_config('app.user_id', $1, true)", str(user_id or ""))
            await conn.execute("SET LOCAL ROLE assistant_ro")
            rows = await conn.fetch(f"SELECT * FROM ({sql}) AS q LIMIT 200")
    return [dict(r) for r in rows]


async def _family_names(user: users.User) -> str:
    return ", ".join(m.name for m in await users.family_members(user.family_id))


async def answer(user: users.User, question: str, header: str = "") -> Reply:
    s = get_settings()
    n = now()
    system = SQL_WRITER.format(
        now=f"{n:%Y-%m-%d %H:%M} ({WEEKDAYS[n.weekday()]})", tz=s.tz, user_name=user.name,
        family=await _family_names(user), expense_cats=", ".join(await finance.category_names("expense")))
    prompt = question
    rows, sql, last_err = None, None, ""
    for _attempt in range(2):     # если запрос не сработал, ИИ получает ошибку и исправляет его
        data = await ai.complete_json(system, prompt)
        sql = data.get("sql")
        if not sql:
            reason = data.get("reason") or "по записанным данным на это не ответить"
            return Reply(text=(header + "\n" if header else "") + f"🤔 {esc(reason)}")
        try:
            rows = await run_sql(user.family_id, sql, user.id)
            break
        except Exception as e:   # ошибка проверки или ошибка PostgreSQL
            last_err = str(e)[:300]
            log.info("SQL не сработал (%s): %s", last_err, sql)
            prompt = f"{question}\n\nПредыдущий запрос:\n{sql}\nОшибка: {last_err}\nИсправь запрос."
    if rows is None:
        return Reply(text=(header + "\n" if header else "") +
                     "⚠️ Не получилось найти ответ в данных. Попробуйте спросить иначе.")

    result = json.dumps(rows[:200], ensure_ascii=False, default=str)
    text = await ai.complete(DATA_ANSWER.format(now=f"{n:%Y-%m-%d %H:%M}", currency=s.currency),
                             f"Вопрос: {question}\nSQL: {sql}\nРезультат ({len(rows)} строк): {result[:12000]}",
                             max_tokens=800)
    return Reply(text=(header + "\n" if header else "") + "🔎 " + esc(text.strip()))
