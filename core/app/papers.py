"""Документы, полисы и гарантии со сроками: паспорта, ОСАГО, техосмотр, гарантия на технику.

Хранится только название, чей документ и срок. Номера документов не сохраняются никогда.
Фото (гарантийный талон, полис) можно приложить — оно хранится на вашем сервере и открывается в панели.
Напоминания: документы — за 90, 30 и 7 дней; полисы — за 30, 7 и 1 день; гарантии — за 30 и 7 дней.
"""
from datetime import date

from . import db, media, notify, scope
from .config import get_settings
from .schemas import Button, Reply
from .users import User
from .util import add_months, esc, now, num, parse_date, today

KINDS = {"document": "🪪 Документ", "policy": "📑 Полис", "warranty": "🧾 Гарантия"}
THRESHOLDS = {"document": [90, 30, 7], "policy": [30, 7, 1], "warranty": [30, 7]}


async def save_attachment(family_id: int, data: bytes, mime: str) -> int:
    data, mime = media.compress(data, mime)      # 1.10.3: фото хранится сжатым
    return await db.get_pool().fetchval("INSERT INTO attachments (family_id, mime, data) VALUES ($1,$2,$3) RETURNING id",
                                         family_id, mime, data)


async def list_all(family_id: int) -> list[dict]:
    rows = await db.get_pool().fetch(
        "SELECT p.*, v.name AS vehicle FROM papers p LEFT JOIN vehicles v ON v.id=p.vehicle_id"
        f" WHERE p.family_id=$1 AND p.deleted_at IS NULL AND {scope.cond('papers', 'p.user_id')}"
        " ORDER BY p.expires_on NULLS LAST, p.title", family_id)
    return [dict(r) for r in rows]


async def for_vehicle(family_id: int, vid: int) -> list[dict]:
    return [p for p in await list_all(family_id) if p["vehicle_id"] == vid]


def days_left(p: dict) -> int | None:
    return (p["expires_on"] - today()).days if p["expires_on"] else None


def _icon(left: int | None) -> str:
    if left is None:
        return "⚪"
    return "🔴" if left < 0 else "🟠" if left <= 30 else "🟡" if left <= 90 else "🟢"


def short_line(p: dict) -> str:
    left = days_left(p)
    when = "без срока" if left is None else (f"истёк {p['expires_on']:%d.%m.%Y}" if left < 0
                                              else f"до {p['expires_on']:%d.%m.%Y} ({left} дн.)")
    return f"{_icon(left)} {esc(p['title'])} — {when}"


def line(p: dict) -> str:
    who = p["owner"] or p.get("vehicle")
    return short_line(p) + (f" · {esc(who)}" if who else "") + (" 📎" if p["attachment_id"] else "")


async def save(user: User, data: dict, attachment_id: int | None = None) -> Reply:
    from . import garage
    kind = data.get("kind") if data.get("kind") in KINDS else "document"
    title = str(data.get("title") or "").strip()[:120]
    if not title:
        return Reply(text="Что за документ? Пример: <code>ОСАГО на машину до 15.05.2027</code>, "
                          "<code>гарантия на холодильник 2 года с 12.03.2026</code>")
    purchased = parse_date(data.get("purchased_on"))
    expires = parse_date(data.get("expires_on"))
    months = num(data.get("warranty_months"))
    if not expires and purchased and months:
        expires = add_months(purchased, int(months))
    owner = (str(data.get("owner") or "").strip() or None)
    v = await garage.find(user.family_id, data.get("vehicle")) if data.get("vehicle") else None
    pool = db.get_pool()
    # Продление: тот же документ того же владельца — обновляем срок, а не создаём новый
    same = await pool.fetchrow(
        "SELECT id FROM papers WHERE family_id=$1 AND deleted_at IS NULL AND lower(title)=lower($2)"
        " AND coalesce(lower(owner),'')=coalesce(lower($3),'') AND coalesce(vehicle_id,0)=coalesce($4,0)",
        user.family_id, title, owner, v["id"] if v else None)
    if not same and data.get("renew"):
        # «ОСАГО продлил до…»: ищем похожий документ по словам названия
        stems = [w[:max(4, len(w) - 2)] for w in title.lower().replace("ё", "е").split() if len(w) >= 4]
        cands = [r for r in await pool.fetch(
            "SELECT id, title, owner, vehicle_id FROM papers WHERE family_id=$1 AND deleted_at IS NULL AND kind=$2"
            " ORDER BY expires_on NULLS LAST", user.family_id, kind)
            if (not owner or (r["owner"] or "").lower() == owner.lower())
            and (not v or r["vehicle_id"] == v["id"])]
        name = lambda r: r["title"].lower().replace("ё", "е")
        full = [r for r in cands if stems and all(s in name(r) for s in stems)]
        part = [r for r in cands if any(s in name(r) for s in stems)]
        # все слова совпали — берём; иначе одно похожее, если оно единственное у этого владельца/техники
        same = (full or (part if len(part) == 1 else [None]))[0]
    if same:
        await pool.execute("UPDATE papers SET expires_on=coalesce($2,expires_on), purchased_on=coalesce($3,purchased_on),"
                           " note=coalesce($4,note), attachment_id=coalesce($5,attachment_id), reminded_days=NULL WHERE id=$1",
                           same["id"], expires, purchased, data.get("note"), attachment_id)
        pid, head = same["id"], "✏️ Обновил срок"
    else:
        pid = await pool.fetchval(
            "INSERT INTO papers (family_id, user_id, kind, title, owner, vehicle_id, expires_on, purchased_on, note, attachment_id)"
            " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id", user.family_id, user.id, kind, title, owner,
            v["id"] if v else None, expires, purchased, (str(data.get("note") or "").strip()[:300] or None), attachment_id)
        head = "📄 Сохранил"
    p = next(x for x in await list_all(user.family_id) if x["id"] == pid)
    text = f"{head}: {KINDS[p['kind']]}\n{line(p)}"
    if p["expires_on"]:
        text += f"\nНапомню за {', '.join(str(t) for t in THRESHOLDS[p['kind']])} дн. до конца срока."
    else:
        text += "\nСрок не указан. Добавьте: <code>" + esc(title) + " до 15.05.2027</code>"
    return Reply(text=text, buttons=[[Button(text="📄 Все документы", action="d:0:list")]])


async def list_reply(user: User) -> Reply:
    ps = await list_all(user.family_id)
    if not ps:
        return Reply(text="📄 Документов пока нет.\nПримеры: <code>паспорт Яны до 2031-05-01</code>, "
                          "<code>ОСАГО на мотоцикл до 20.04.2027</code>, фото гарантийного талона с подписью «гарантия».\n"
                          "Номера документов я не храню — только название и срок.")
    groups = {}
    for p in ps:
        groups.setdefault(p["kind"], []).append(p)
    text = "📄 <b>Документы и сроки</b>"
    for k in ("document", "policy", "warranty"):
        if groups.get(k):
            text += f"\n\n<b>{KINDS[k]}</b>\n" + "\n".join(line(p) for p in groups[k])
    return Reply(text=text)


async def handle_action(user: User, parts: list[str]) -> Reply:
    if parts[2] == "list":
        return await list_reply(user)
    return Reply(text="Неизвестное действие.")


async def send_due() -> None:
    n = now()
    if n.hour < get_settings().daily_reminder_hour:
        return
    rows = await db.get_pool().fetch("SELECT * FROM papers WHERE deleted_at IS NULL AND expires_on IS NOT NULL"
                                     " AND expires_on <= current_date + 90")
    for r in rows:
        p = dict(r)
        left = days_left(p)
        hit = [t for t in THRESHOLDS[p["kind"]] if left <= t]
        if not hit or left < -1:
            continue
        t = min(hit)
        if p["reminded_days"] is not None and p["reminded_days"] <= t:
            continue
        ok = await db.get_pool().fetchval(
            "UPDATE papers SET reminded_days=$2 WHERE id=$1 AND (reminded_days IS NULL OR reminded_days > $2) RETURNING id",
            p["id"], t)
        if not ok:
            continue
        what = "истекает сегодня" if left == 0 else "истёк" if left < 0 else f"истекает через {left} дн."
        tip = {"document": "Запишитесь на замену заранее (Госуслуги).",
               "policy": "Продлите заранее, чтобы не ездить без полиса.",
               "warranty": "Если есть претензии к технике — успейте обратиться по гарантии."}[p["kind"]]
        who = f" ({esc(p['owner'])})" if p["owner"] else ""
        await notify.remind_about("papers", p["family_id"], p["user_id"], Reply(
            text=f"📄 <b>{esc(p['title'])}</b>{who} {what} — {p['expires_on']:%d.%m.%Y}.\n{tip}\n"
                 f"Когда продлите, напишите: <code>{esc(p['title'])} продлил до ДД.ММ.ГГГГ</code>"))


async def morning_lines(user: User) -> list[str]:
    return [f"📄 {short_line(p)}" for p in await list_all(user.family_id)
            if days_left(p) is not None and days_left(p) <= 14][:3]
