"""Рецепты и меню: по запросу или по фото продуктов. Недостающее уходит в список покупок."""
from . import ai, db, nutrition, shopping
from .config import get_settings
from .prompts import RECIPE
from .schemas import Button, Reply
from .users import User
from .util import esc, today


async def suggest(user: User, request: str, products: list[str] | None = None, header: str = "") -> Reply:
    t = await nutrition.day_totals(user, today())
    left = int(nutrition.goal_for(user) - t["kcal"])
    system = RECIPE.format(request=request or "что приготовить", kcal_left=left,
                           products=", ".join(products) if products else "неизвестно",
                           family_desc=get_settings().family_desc)
    data = await ai.complete_json(system, request or "Предложи рецепты", max_tokens=3000)
    recipes = [r for r in (data.get("recipes") or []) if isinstance(r, dict) and r.get("title")][:3]
    if not recipes:
        return Reply(text="🤔 Не получилось придумать рецепт. Уточните, что есть из продуктов.")
    sid = await db.get_pool().fetchval(
        "INSERT INTO suggestions (user_id, kind, data) VALUES ($1, 'recipes', $2) RETURNING id",
        user.id, {"recipes": recipes})

    text = (header + "\n" if header else "") + "👩‍🍳 <b>Что приготовить</b>\n"
    if products:
        text += f"<i>Есть: {esc(', '.join(products[:15]))}</i>\n"
    buttons = []
    for n, r in enumerate(recipes):
        missing = [i for i in r.get("ingredients") or [] if not i.get("have")]
        text += (f"\n<b>{n + 1}. {esc(r['title'])}</b> · {r.get('time_min', '?')} мин\n"
                 f"🔥 {_i(r.get('kcal'))} ккал/порция · Б {_i(r.get('protein'))} · Ж {_i(r.get('fat'))} · У {_i(r.get('carbs'))}\n")
        text += "🧺 " + ", ".join(esc(f"{i.get('name')} {i.get('qty') or ''}".strip())
                                 for i in r.get("ingredients") or []) + "\n"
        for k, step in enumerate((r.get("steps") or [])[:6], 1):
            text += f"  {k}) {esc(str(step))}\n"
        if missing:
            text += "🛒 Не хватает: " + ", ".join(esc(str(i.get("name"))) for i in missing) + "\n"
        row = [Button(text=f"🍽 Съел {n + 1}", action=f"r:{sid}:eat:{n}")]
        if missing:
            row.insert(0, Button(text=f"🛒 Купить для {n + 1}", action=f"r:{sid}:shop:{n}"))
        buttons.append(row)
    if left > 0:
        text += f"\nОсталось на сегодня: {left} ккал"
    return Reply(text=text[:4000], buttons=buttons)


def _i(v) -> str:
    try:
        return str(int(float(v)))
    except (TypeError, ValueError):
        return "?"


async def handle_action(user: User, parts: list[str]) -> Reply:
    """r:<suggestion_id>:shop:<n> | r:<suggestion_id>:eat:<n>"""
    row = await db.get_pool().fetchrow("SELECT data FROM suggestions WHERE id=$1 AND user_id=$2",
                                       int(parts[1]), user.id)
    try:
        r = row["data"]["recipes"][int(parts[3])]
    except (TypeError, KeyError, IndexError, ValueError):
        return Reply(text="Рецепт не найден, попросите новый.")
    if parts[2] == "shop":
        items = [{"title": i.get("name"), "qty": i.get("qty")} for i in r.get("ingredients") or [] if not i.get("have")]
        return await shopping.add(user, items)
    if parts[2] == "eat":
        meal = await nutrition.save(user, {"title": r["title"], "kcal": r.get("kcal"), "protein": r.get("protein"),
                                           "fat": r.get("fat"), "carbs": r.get("carbs")}, source="recipe")
        return await nutrition.render_saved(user, meal, header="👩‍🍳 Записал порцию по рецепту")
    return Reply(text="Неизвестное действие.")
