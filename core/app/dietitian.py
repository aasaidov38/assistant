"""Режим диетолога (версия 1.10.6): разбор питания за неделю по «Гарвардской тарелке» и вопросы про питание.

Что делает.
- `week_reply(user)` / `week_data(user)`: цифры за 7 дней считает код (калории, белки, жиры, углеводы, доли калорий,
  дни выше цели, перекусы), а по названиям съеденного ИИ оценивает, как это выглядит относительно тарелки:
  ½ овощи и фрукты (картофель не считается), ¼ цельные злаки, ¼ полезный белок (рыба, птица, бобовые, орехи;
  красное мясо — реже, колбасы и бекон — не стоит), полезные растительные масла, вода вместо сладких напитков.
  Источник принципов: Healthy Eating Plate, Harvard T.H. Chan School of Public Health.
- `ask(user, вопрос)`: ответы диетолога на вопросы про питание, КБЖУ продуктов и диеты (в основе — та же тарелка).
- Режим диетолога — настройка `dietitian` в users.settings (по умолчанию включён): если включён, в день недельного
  разбора после него приходит отдельное сообщение с разбором по тарелке. Вопросы и /diet работают всегда.

Границы (записаны и в запросе к ИИ): оценка идёт по названиям блюд, порции не взвешены — ассистент так и говорит;
не ставит диагнозов, не советует БАДы и дозы витаминов, не подбирает лечебные диеты (при болезнях, беременности,
детском питании — к врачу или диетологу); не подталкивает к похудению и не хвалит недоедание: если по записям калорий
явно мало, мягко напоминает, что записи могут быть неполными, и советует обсудить рацион с врачом.
Кэш разбора — в app_state, ключ `diet:week:<id>` (повторное нажатие без новых записей бесплатно).
"""
import hashlib
import json
import logging
import re
import time
from collections import Counter
from datetime import timedelta

from . import ai, db, nutrition, prefs, release
from .config import get_settings
from .schemas import Reply
from .users import User
from .util import esc, today

log = logging.getLogger("dietitian")

MIN_DAYS = 3                    # меньше дней с записями — оценку по тарелке не делаем
DAILY_LIMIT = 40                # обращений к ИИ (вопросы и новые разборы) на человека в сутки
LOW_SHARE = 0.7                 # среднее меньше 70% цели — предупредить мягко
NOTE = "Общая информация о питании, не заменяет консультацию врача или диетолога."
STATUS = {"в норме": "🟢", "мало": "🟠", "много": "🟠", "нет данных": "⚪"}
_used: dict[tuple[int, str], int] = {}

PLATE = """Принципы «Гарвардской тарелки» (Healthy Eating Plate, Гарвардская школа общественного здравоохранения):
- Овощи и фрукты — половина тарелки, чем разнообразнее и ярче, тем лучше. Картофель и картофель фри овощами не считаются.
- Цельнозерновые (овсянка, цельнозерновой хлеб и паста, бурый рис, гречка, булгур, киноа) — четверть тарелки;
  белый хлеб, белый рис и другие рафинированные зерна — реже.
- Полезный белок — четверть тарелки: рыба, птица, яйца, бобовые, орехи и семена. Красное мясо и сыр — ограничивать,
  колбасы, бекон, сосиски и другие переработанные мясные продукты — избегать.
- Полезные растительные масла (оливковое, рапсовое, подсолнечное) — в умеренном количестве, сливочное — реже, транс-жиров избегать.
  Гарвард не ограничивает долю калорий из полезных жиров: важнее качество, а не процент.
- Напитки: вода, чай, кофе без сахара; молоко и молочное — 1–2 порции в день, сок — не больше маленького стакана; сладкие напитки не стоит.
- Тип углеводов важнее их количества. Размеры частей тарелки — ориентир пропорций, а не число калорий.
- Двигаться каждый день."""

REVIEW = PLATE + """

Ты диетолог в семейном приложении. Ниже JSON: статистика питания человека за неделю (цифры уже посчитаны, их не пересчитывай)
и список «foods» (названия съеденного с числом упоминаний). Оцени по тарелке, что видно ПО НАЗВАНИЯМ. Порции не взвешены, записи могут быть
неполными: говори «по записям похоже», а не «вы едите». Верни ОДИН JSON-объект без пояснений:
{"plate": [
  {"part": "Овощи и фрукты", "status": "в норме|мало|много|нет данных", "comment": "до 120 символов, с примерами из списка"},
  {"part": "Цельнозерновые", "status": "...", "comment": "..."},
  {"part": "Белок", "status": "...", "comment": "что за белок в записях и чего не хватает"},
  {"part": "Масла и жиры", "status": "...", "comment": "..."},
  {"part": "Напитки и сладкое", "status": "...", "comment": "сладкие напитки, сладости, переработанное мясо — только если они есть в списке"}],
 "good": ["1–3 коротких пункта, что получается хорошо"],
 "improve": ["1–3 коротких пункта, что стоит улучшить"],
 "steps": ["2–3 простых шага на следующую неделю, конкретные продукты и блюда"],
 "note": "одно предложение, если нужно (например, про неполные записи) или пустая строка"}
Правила:
- Говори только о продуктах из списка foods; ничего не выдумывай. Если по списку нельзя судить, ставь «нет данных».
- Тон дружелюбный, без стыда и осуждения, без слов «плохой» и «вредный» про людей и их выбор. Пиши «лучше реже», «стоит добавить».
- Не называй свои цифры калорий, не советуй худеть или сокращать калории, не хвали недоедание. Цель по калориям задаёт сам человек.
- Если в статистике low_intake = true, в note мягко скажи: записи могут быть неполными, а если едите так на самом деле, обсудите рацион с врачом или диетологом.
- Не ставь диагнозов, не советуй БАДы, витамины и лекарства. При болезнях, беременности, детском питании в note посоветуй врача.
- Пункты короткие, до 140 символов, без Markdown."""

ASK = PLATE + """

Ты диетолог в семейном приложении. Отвечай по-русски, простым языком, коротко (до 900 символов), без Markdown и без приветствия.
Тебе дают вопрос и контекст человека в JSON (цель по калориям, итоги за сегодня, средние за неделю — используй, только если это помогает ответу).
Правила:
- На вопросы про КБЖУ продуктов давай примерные значения на 100 г или на типичную порцию и пиши «примерно»: у разных продуктов и марок цифры отличаются.
- Опирайся на тарелку и общепринятые данные о питании. Если вопрос спорный или данных мало, так и скажи.
- Не ставь диагнозов и не подбирай лечебные диеты. При болезнях (диабет, болезни почек, печени, ЖКТ), беременности, кормлении, питании детей, аллергии
  посоветуй обсудить с врачом или диетологом, а общие принципы дай как справку.
- Не советуй БАДы, дозы витаминов и лекарства, не предлагай голодание, жёсткие ограничения и «быстрое похудение».
  Если человек пишет о страхе еды, срывах, наказании себя едой или резком недоедании, поддержи, не давай цифр и советуй обратиться к врачу или психологу.
- Если вопрос не про питание, ответь одной фразой, что отвечаешь только про питание."""


# ---------- данные ----------

def _spend(user: User) -> bool:
    key = (user.id, time.strftime("%Y-%m-%d"))
    for k in [k for k in _used if k[1] != key[1]]:
        _used.pop(k, None)
    if _used.get(key, 0) >= DAILY_LIMIT:
        return False
    _used[key] = _used.get(key, 0) + 1
    return True


def _items(raw) -> list:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return raw if isinstance(raw, list) else []


async def week_data(user: User) -> dict:
    """Статистика питания за последние 7 дней (включая сегодня). days_logged = 0, если записей нет."""
    z = get_settings().zone
    end = today()
    start = end - timedelta(days=6)
    rows = await db.get_pool().fetch(
        "SELECT (eaten_at AT TIME ZONE $4)::date AS day, meal_type, title, kcal, protein, fat, carbs, items FROM meals"
        " WHERE user_id=$1 AND deleted_at IS NULL AND (eaten_at AT TIME ZONE $4)::date >= $2"
        " AND (eaten_at AT TIME ZONE $4)::date <= $3 ORDER BY eaten_at", user.id, start, end, str(z))
    goal = nutrition.goal_for(user)
    out = {"from": start.isoformat(), "to": end.isoformat(), "days_logged": 0, "kcal_goal": goal}
    if not rows:
        return out
    days: dict = {}
    foods: Counter = Counter()
    snack = 0.0
    for r in rows:
        d = days.setdefault(r["day"], {"kcal": 0.0, "p": 0.0, "f": 0.0, "c": 0.0})
        for k, col in (("kcal", "kcal"), ("p", "protein"), ("f", "fat"), ("c", "carbs")):
            d[k] += float(r[col] or 0)
        if r["meal_type"] == "snack":
            snack += float(r["kcal"] or 0)
        names = [str(i.get("name")).strip().lower() for i in _items(r["items"]) if isinstance(i, dict) and i.get("name")]
        for n in names or [str(r["title"] or "").strip().lower()]:
            if n:
                foods[n[:60]] += 1
    n = len(days)
    tot = {k: sum(d[k] for d in days.values()) for k in ("kcal", "p", "f", "c")}
    avg = {k: round(v / n) for k, v in tot.items()}
    energy = tot["p"] * 4 + tot["f"] * 9 + tot["c"] * 4
    share = ({"protein": round(tot["p"] * 4 * 100 / energy), "fat": round(tot["f"] * 9 * 100 / energy),
              "carbs": round(tot["c"] * 4 * 100 / energy)} if energy else {})
    out.update({
        "days_logged": n, "avg_kcal": avg["kcal"], "avg_protein": avg["p"], "avg_fat": avg["f"], "avg_carbs": avg["c"],
        "macro_share_pct": share, "days_over_goal": sum(1 for d in days.values() if d["kcal"] > goal),
        "snack_share_pct": round(snack * 100 / tot["kcal"]) if tot["kcal"] else 0,
        "low_intake": n >= MIN_DAYS and avg["kcal"] < goal * LOW_SHARE,
        "foods": [{"name": k, "times": v} for k, v in foods.most_common(60)]})
    return out


# ---------- разбор недели ----------

def build_review(j) -> dict | None:
    """Ответ модели → безопасный разбор. None, если ответ непригоден."""
    if not isinstance(j, dict) or not isinstance(j.get("plate"), list):
        return None
    plate = []
    for p in j["plate"][:6]:
        if isinstance(p, dict) and p.get("part"):
            st = str(p.get("status") or "").strip().lower()
            plate.append({"part": _c(p["part"], 40), "status": st if st in STATUS else "нет данных",
                          "comment": _c(p.get("comment"), 160)})
    if not plate:
        return None
    lists = {k: [_c(x, 160) for x in (j.get(k) if isinstance(j.get(k), list) else [j.get(k)] if j.get(k) else []) if _c(x, 160)][:3]
             for k in ("good", "improve", "steps")}
    return {"plate": plate, **lists, "note": _c(j.get("note"), 240)}


def _c(v, limit: int) -> str:
    return re.sub(r"[*_`#]+", "", re.sub(r"\s+", " ", str(v or ""))).strip()[:limit]


async def get_week(user: User) -> dict:
    """{"stats": {...}, "review": {...}|None, "reason": None|"no_data"|"few_days"|"limit"|"error"}."""
    stats = await week_data(user)
    if not stats["days_logged"]:
        return {"stats": stats, "review": None, "reason": "no_data"}
    if stats["days_logged"] < MIN_DAYS:
        return {"stats": stats, "review": None, "reason": "few_days"}
    digest = hashlib.md5(json.dumps(stats, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    key = f"diet:week:{user.id}"
    hit = await release.get_state(key)
    if hit.get("review") and hit.get("hash") == digest:
        return {"stats": stats, "review": hit["review"], "reason": None}
    if not _spend(user):
        return {"stats": stats, "review": None, "reason": "limit"}
    try:
        review = build_review(await ai.complete_json(REVIEW, json.dumps(stats, ensure_ascii=False), max_tokens=1500))
    except Exception as e:  # noqa: BLE001
        log.warning("разбор питания не получен: %s", e)
        return {"stats": stats, "review": None, "reason": "error"}
    if not review:
        return {"stats": stats, "review": None, "reason": "error"}
    await release.set_state(key, {"hash": digest, "review": review, "at": time.time()})
    return {"stats": stats, "review": review, "reason": None}


def stats_text(s: dict) -> str:
    a, b = s["from"], s["to"]
    head = f"🥗 <b>Питание за неделю</b> ({a[8:]}.{a[5:7]}–{b[8:]}.{b[5:7]})"
    if not s["days_logged"]:
        return head
    t = (f"{head}\nЗаписи: {s['days_logged']} из 7 дней\n"
         f"В среднем {s['avg_kcal']} ккал (цель {s['kcal_goal']}) · Б {s['avg_protein']} · Ж {s['avg_fat']} · У {s['avg_carbs']} г")
    sh = s.get("macro_share_pct") or {}
    if sh:
        t += f"\nДоли калорий: белки {sh['protein']}% · жиры {sh['fat']}% · углеводы {sh['carbs']}%"
    t += f"\nДней выше цели: {s['days_over_goal']} · доля перекусов: {s['snack_share_pct']}% калорий"
    return t


REASONS = {
    "no_data": "За последние 7 дней нет записей о еде. Пришлите фото тарелки или напишите, что съели, и через несколько дней сделаю разбор.",
    "few_days": f"Записей пока мало: для оценки по тарелке нужно хотя бы {MIN_DAYS} дня с записями за неделю. Продолжайте записывать еду.",
    "limit": "На сегодня лимит обращений к диетологу исчерпан. Попробуйте завтра (уже готовый разбор откроется сразу).",
    "error": "Разбор сейчас недоступен (не отвечает ИИ). Попробуйте позже.",
}


def review_text(r: dict) -> str:
    s, rv = r["stats"], r["review"]
    if not rv:
        body = stats_text(s) if s["days_logged"] else "🥗 <b>Питание за неделю</b>"
        return body + "\n\n" + esc(REASONS[r["reason"]])
    parts = [stats_text(s), "<b>Тарелка по схеме Гарварда</b> <i>(по названиям блюд, порции не взвешены)</i>\n"
             + "\n".join(f"{STATUS[p['status']]} <b>{esc(p['part'])}</b>: {esc(p['status'])}" + (f". {esc(p['comment'])}" if p["comment"] else "")
                         for p in rv["plate"])]
    for key, title in (("good", "Что получается"), ("improve", "Что улучшить"), ("steps", "Шаги на неделю")):
        if rv[key]:
            parts.append(f"<b>{title}</b>\n" + "\n".join(f"• {esc(x)}" for x in rv[key]))
    if rv["note"]:
        parts.append(esc(rv["note"]))
    parts.append("<i>" + esc(NOTE) + "</i>")
    return "\n\n".join(parts)[:4000]


async def week_reply(user: User, auto: bool = False) -> Reply | None:
    """Разбор для бота. auto=True (в день недельного разбора): без записей или с малым числом дней ничего не шлём."""
    r = await get_week(user)
    if auto and r["reason"] in ("no_data", "few_days", "limit", "error"):
        return None
    return Reply(text=review_text(r))


# ---------- вопросы ----------

async def ask(user: User, question) -> Reply:
    q = re.sub(r"\s+", " ", str(question or "")).strip()[:500]
    if not q:
        return Reply(text="Задайте вопрос про питание: <code>/diet сколько белка в твороге?</code> или <code>/diet как набрать белок без мяса</code>")
    if not _spend(user):
        return Reply(text=REASONS["limit"])
    ctx = {"kcal_goal": nutrition.goal_for(user)}
    try:
        w = await week_data(user)
        if w["days_logged"]:
            ctx["week_avg"] = {k: w[k] for k in ("avg_kcal", "avg_protein", "avg_fat", "avg_carbs", "days_logged")}
        t = await nutrition.day_totals(user, today())
        if t["n"]:
            ctx["today"] = {"kcal": round(float(t["kcal"])), "protein": round(float(t["protein"])),
                            "fat": round(float(t["fat"])), "carbs": round(float(t["carbs"]))}
        raw = await ai.complete(ASK, json.dumps({"question": q, "context": ctx}, ensure_ascii=False), max_tokens=700)
    except Exception as e:  # noqa: BLE001
        log.warning("ответ диетолога не получен: %s", e)
        return Reply(text=REASONS["error"])
    text = _c_multiline(raw)
    if not text:
        return Reply(text=REASONS["error"])
    return Reply(text="🥗 " + esc(text) + "\n\n<i>" + esc(NOTE) + "</i>")


def _c_multiline(v) -> str:
    lines = [re.sub(r"[*_`#]+", "", ln).rstrip() for ln in str(v or "").strip().splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:1800]


# ---------- режим и справка ----------

async def enabled(user: User) -> bool:
    return bool((await prefs.get(user.id)).get("dietitian", True))


async def set_mode(user: User, on: bool) -> Reply:
    await prefs.update(user, {"dietitian": on})
    return Reply(text=("🥗 Режим диетолога включён: в день недельного разбора пришлю ещё и разбор питания по тарелке Гарварда."
                       if on else "🥗 Режим диетолога выключен: разбор питания по тарелке сам приходить не будет. Вопросы и /diet week работают."))


async def intro(user: User) -> Reply:
    on = await enabled(user)
    return Reply(text=(
        "🥗 <b>Режим диетолога</b>: " + ("включён" if on else "выключен") + "\n"
        "Опирается на тарелку Гарварда: ½ овощи и фрукты (картофель не считается), ¼ цельные злаки, ¼ полезный белок "
        "(рыба, птица, бобовые, орехи), полезные масла, вода вместо сладких напитков.\n\n"
        "<code>/diet week</code>: разбор моего питания за неделю\n"
        "<code>/diet сколько белка в твороге?</code>: вопрос диетологу (можно и без команды: «можно ли есть яйца каждый день?»)\n"
        "<code>/diet on</code> · <code>/diet off</code>: присылать ли разбор по тарелке вместе с недельным разбором\n\n"
        "<i>" + esc(NOTE) + "</i>"))


async def command(user: User, arg: str | None) -> Reply:
    a = re.sub(r"\s+", " ", (arg or "").strip())
    low = a.lower().replace("ё", "е")
    if not a:
        return await intro(user)
    if low in ("week", "неделя", "за неделю", "разбор", "разбор недели", "тарелка"):
        return await week_reply(user)
    if low in ("on", "вкл", "включить", "включи"):
        return await set_mode(user, True)
    if low in ("off", "выкл", "выключить", "выключи"):
        return await set_mode(user, False)
    return await ask(user, a)
