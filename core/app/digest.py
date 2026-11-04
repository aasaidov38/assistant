"""Недельный разбор: цифры за 7 дней + 2–3 совета от ИИ.

Приходит сам по расписанию (WEEKLY_DIGEST_DAY / WEEKLY_DIGEST_HOUR в .env) и по команде /week.
"""
import json
import logging
from datetime import date, timedelta

from . import ai, budgets, db, dietitian, finance, habits, notify, nutrition, prefs, scope, tasks, users
from .config import get_settings
from .prompts import DIGEST_ADVICE
from .schemas import Reply
from .util import WEEKDAYS, esc, now, today

log = logging.getLogger("digest")


async def _expenses(family_id: int, a: date, b: date) -> dict[str, float]:
    rows = await db.get_pool().fetch(
        "SELECT c.emoji || ' ' || c.name AS cat, sum(t.amount) AS s FROM transactions t"
        " JOIN categories c ON c.id=t.category_id WHERE t.family_id=$1 AND t.kind='expense'"
        " AND t.deleted_at IS NULL AND t.occurred_on >= $2 AND t.occurred_on < $3"
        f" AND {scope.cond('finance', 't.user_id')} GROUP BY 1 ORDER BY 2 DESC",
        family_id, a, b)
    return {r["cat"]: float(r["s"]) for r in rows}


async def _meals(user: users.User, a: date, b: date) -> dict:
    z = get_settings().zone
    rows = await db.get_pool().fetch(
        "SELECT (eaten_at AT TIME ZONE $4)::date AS day, sum(kcal) AS kcal, sum(protein) AS p,"
        " sum(fat) AS f, sum(carbs) AS c FROM meals WHERE user_id=$1 AND deleted_at IS NULL"
        " AND (eaten_at AT TIME ZONE $4)::date >= $2 AND (eaten_at AT TIME ZONE $4)::date < $3 GROUP BY 1",
        user.id, a, b, str(z))
    if not rows:
        return {"days_logged": 0}
    n = len(rows)
    return {"days_logged": n, "kcal_goal": nutrition.goal_for(user),
            "avg_kcal": round(sum(float(r["kcal"]) for r in rows) / n),
            "avg_protein": round(sum(float(r["p"]) for r in rows) / n),
            "avg_fat": round(sum(float(r["f"]) for r in rows) / n),
            "avg_carbs": round(sum(float(r["c"]) for r in rows) / n),
            "days_over_goal": sum(1 for r in rows if float(r["kcal"]) > nutrition.goal_for(user))}


async def build(user: users.User) -> Reply:
    async with scope.as_user(user):
        return await _build(user)


async def _build(user: users.User) -> Reply:
    end = today() + timedelta(days=1)          # включая сегодня
    start = end - timedelta(days=7)
    prev = start - timedelta(days=7)
    cur = await _expenses(user.family_id, start, end)
    old = await _expenses(user.family_id, prev, start)
    meals = await _meals(user, start, end)
    budget = [{"category": r["name"], "spent": float(r["spent"]), "limit": float(r["lim"])}
              for r in await budgets.status_rows(user.family_id)]
    late = await tasks.overdue(user.family_id)
    done = await db.get_pool().fetchval(
        "SELECT count(*) FROM tasks WHERE family_id=$1 AND status='done' AND done_at >= now() - interval '7 days'",
        user.family_id)

    total, total_old = sum(cur.values()), sum(old.values())
    text = f"🗓 <b>Неделя {start:%d.%m}–{(end - timedelta(days=1)):%d.%m}</b>\n\n💸 <b>{'Мои траты' if scope.is_personal('finance') else 'Траты семьи'}: {finance.money(total)}</b>"
    if total_old:
        diff = round((total - total_old) * 100 / total_old)
        text += f" ({'+' if diff >= 0 else ''}{diff}% к прошлой неделе)"
    text += "\n"
    for cat, v in list(cur.items())[:6]:
        o = old.get(cat, 0)
        arrow = "" if not o else (" ↑" if v > o * 1.15 else " ↓" if v < o * 0.85 else "")
        text += f"{esc(cat)}: {finance.money(v)}{arrow}\n"
    if budget:
        text += "\n🎯 <b>Лимиты месяца</b>\n"
        for b in budget:
            pct = int(b["spent"] * 100 / b["limit"])
            text += f"{'🟥' if pct >= 100 else '🟨' if pct >= 80 else '🟩'} {esc(b['category'])}: {pct}%\n"
    if meals["days_logged"]:
        text += (f"\n🍽 <b>Питание</b> ({meals['days_logged']} из 7 дн. с записями)\n"
                 f"В среднем {meals['avg_kcal']} ккал из {meals['kcal_goal']} · "
                 f"Б {meals['avg_protein']} · Ж {meals['avg_fat']} · У {meals['avg_carbs']}\n")
    hab = await habits.week_stats(user)
    if hab:
        parts = []
        for kind in ("water", "steps", "sleep"):
            if kind in hab:
                h = hab[kind]
                parts.append(f"{habits.KINDS[kind][0]} {habits.fmt(kind, h['avg'])}" +
                             (f" из {habits.fmt(kind, h['goal'])}" if h["goal"] else ""))
        if "weight" in hab:
            w = hab["weight"]
            dv = f" ({w['now'] - w['week_ago']:+.1f})".replace(".", ",") if w["week_ago"] else ""
            parts.append(f"⚖️ {habits.fmt('weight', w['now'])}{dv}")
        text += "📈 В среднем: " + " · ".join(parts) + "\n"
    text += f"\n✅ Дел выполнено: {done}" + (f" · просрочено: {len(late)}" if late else "") + "\n"
    for t in late[:3]:
        text += f"  ⚠️ {esc(t['title'])} ({esc(t['owner'])})\n"

    from . import savings
    sv = await savings.digest_line(user.family_id)
    if sv:
        text += f"\n{sv}\n"

    stats = {"expenses_week": cur, "expenses_prev_week": old, "budgets_month": budget,
             "nutrition": meals, "habits": hab, "tasks_done": done, "tasks_overdue": [t["title"] for t in late]}
    try:
        advice = await ai.complete(DIGEST_ADVICE, json.dumps(stats, ensure_ascii=False, default=str), max_tokens=500)
        if advice.strip():
            text += "\n💡 <b>Советы</b>\n" + esc(advice.strip())
    except ai.AIError as e:
        log.warning("Советы к разбору не получены: %s", e)
    return Reply(text=text[:4000])


async def send_due() -> None:
    """Вызывается фоновой задачей раз в 30 секунд: отправляет разбор, когда наступило время.
    День и час — свои у каждого (настройки в панели), иначе WEEKLY_DIGEST_DAY / WEEKLY_DIGEST_HOUR из .env."""
    s = get_settings()
    n = now()
    rows = await db.get_pool().fetch(
        "SELECT id, coalesce(digest_day, $2) AS day, coalesce(digest_hour, $3) AS hour FROM users"
        " WHERE weekly_digest AND (digest_sent_on IS NULL OR digest_sent_on < $1)",
        n.date(), s.weekly_digest_day, s.weekly_digest_hour)
    for r in rows:
        if r["day"] < 0 or n.weekday() != r["day"] or n.hour < r["hour"]:
            continue
        ok = await db.get_pool().fetchval(
            "UPDATE users SET digest_sent_on=$2 WHERE id=$1 AND (digest_sent_on IS NULL OR digest_sent_on < $2) RETURNING id",
            r["id"], n.date())
        if not ok:
            continue
        user = await users.get(r["id"])
        try:
            await notify.send(user.id, await build(user), urgent=True)
        except Exception:
            log.exception("Не удалось отправить разбор пользователю %s", user.id)
            continue
        try:                                   # 1.10.6: режим диетолога — отдельное сообщение с разбором по тарелке
            if (await prefs.get(user.id)).get("dietitian", True):
                extra = await dietitian.week_reply(user, auto=True)
                if extra:
                    await notify.send(user.id, extra, urgent=False)
        except Exception:
            log.exception("Не удалось отправить разбор по тарелке пользователю %s", user.id)


async def set_enabled(user: users.User, on: bool) -> Reply:
    await db.get_pool().execute("UPDATE users SET weekly_digest=$2 WHERE id=$1", user.id, on)
    s = get_settings()
    when = (f"каждую неделю, {WEEKDAYS[s.weekly_digest_day]}, в {s.weekly_digest_hour}:00"
            if 0 <= s.weekly_digest_day <= 6 else "(расписание выключено в .env)")
    return Reply(text=f"🗓 Недельный разбор {'включён: ' + when if on else 'выключен'}.\n"
                      f"Получить сейчас: /week")
