"""Гараж: машина и мотоцикл. Пробег, регламент ТО, журнал обслуживания, расход на топливо,
сезонные напоминания для Иркутска (резина, консервация мотоцикла).

Страховки и техосмотр хранятся как документы (papers.py) с привязкой к технике.
Кнопки: o:<rule_id>:done | o:<rule_id>:later | o:<vehicle_id>:std | o:0:list
"""
from datetime import date, timedelta
from decimal import Decimal

from . import db, finance, notify, scope
from .config import get_settings
from .schemas import Button, Reply
from .users import User
from .util import add_months, esc, now, num, parse_date, today

KIND_EMOJI = {"moto": "🏍", "car": "🚗"}

# Типовой регламент: (название, каждые км, каждые месяцев)
STANDARD = {
    "moto": [("Замена масла двигателя", 6000, 12), ("Чистка и смазка цепи", 800, None),
             ("Тормозная жидкость", None, 24), ("Свечи зажигания", 12000, None), ("Воздушный фильтр", 12000, 24)],
    "car": [("Замена масла двигателя", 10000, 12), ("Воздушный фильтр", 20000, 24),
            ("Салонный фильтр", 15000, 12), ("Тормозная жидкость", None, 24), ("Свечи зажигания", 30000, None)],
}

# Сезонные напоминания (Иркутск): ключ, вид техники, (месяц, день), текст
SEASON = [
    ("winter_tires", "car", (9, 25), "❄️ <b>Скоро зимняя резина</b>\nВ Иркутске обычно переобуваются в первой половине "
     "октября. Запишитесь в шиномонтаж заранее, пока нет очередей."),
    ("summer_tires", "car", (4, 25), "☀️ <b>Пора думать о летней резине</b>\nОбычно переобуваются с 1 по 15 мая, "
     "когда ночью стабильно выше нуля."),
    ("moto_winter", "moto", (10, 1), "🏍 <b>Консервация мотоцикла на зиму</b>\n• полный бак + стабилизатор топлива\n"
     "• помыть, смазать цепь\n• снять АКБ и держать в тепле на зарядке (или подключить зарядное с режимом хранения)\n"
     "• накачать шины, поставить на подставку\n• закрыть выхлоп и воздухозаборник от грызунов, накрыть чехлом"),
    ("moto_spring", "moto", (4, 15), "🏍 <b>Подготовка мотоцикла к сезону</b>\n• зарядить и поставить АКБ\n"
     "• давление в шинах, износ протектора\n• цепь: натяжение и смазка\n• тормоза: колодки и жидкость\n"
     "• свет и сигналы\n• масло — по регламенту (/garage)"),
]


async def vehicles(family_id: int) -> list[dict]:
    rows = await db.get_pool().fetch(f"SELECT * FROM vehicles WHERE family_id=$1 AND deleted_at IS NULL AND {scope.cond('garage', 'user_id')} ORDER BY id",
        family_id)
    return [dict(r) for r in rows]


async def find(family_id: int, name: str | None) -> dict | None:
    vs = await vehicles(family_id)
    if not vs:
        return None
    n = (name or "").lower().replace("ё", "е").strip()
    if not n:
        return vs[0] if len(vs) == 1 else None
    if n in ("мото", "мотоцикл", "байк", "moto") or n.startswith("мотоц"):
        m = [v for v in vs if v["kind"] == "moto"]
        if len(m) == 1:
            return m[0]
    if n in ("машина", "авто", "автомобиль", "машину", "car") or n.startswith("машин"):
        c = [v for v in vs if v["kind"] == "car"]
        if len(c) == 1:
            return c[0]
    for v in vs:
        if v["name"].lower() == n:
            return v
    stems = [w[:max(3, len(w) - 2)] for w in n.split() if len(w) >= 3]
    return next((v for v in vs if any(s in v["name"].lower().replace("ё", "е") for s in stems)), None)


async def add_vehicle(user: User, data: dict) -> Reply:
    name = str(data.get("name") or "").strip()[:60]
    kind = "moto" if data.get("kind") == "moto" else "car"
    if not name:
        return Reply(text="Как называется техника? Пример: <code>добавь мотоцикл Honda CB400</code>")
    km = num(data.get("km"))
    vid = await db.get_pool().fetchval(
        "INSERT INTO vehicles (family_id, user_id, name, kind, mileage, mileage_at) VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
        user.family_id, user.id, name, kind, int(km) if km else None, now() if km else None)
    if km:
        await db.get_pool().execute("INSERT INTO odometer (vehicle_id, km, user_id) VALUES ($1,$2,$3)", vid, int(km), user.id)
    return Reply(text=f"{KIND_EMOJI[kind]} Добавил: <b>{esc(name)}</b>" + (f", пробег {int(km):,} км".replace(",", " ") if km else "")
                 + "\n\nДобавить типовой регламент ТО? Его потом можно поправить: "
                 "<code>масло мото каждые 5000 км</code>, <code>менял масло в мото на 21000</code>.",
                 buttons=[[Button(text="📋 Добавить типовой регламент", action=f"o:{vid}:std")],
                          [Button(text="🏍 Гараж", action="o:0:list")]])


async def add_standard(user: User, vid: int) -> Reply:
    v = next((x for x in await vehicles(user.family_id) if x["id"] == vid), None)
    if not v:
        return Reply(text="Техника не найдена.")
    pool = db.get_pool()
    have = {r["title"].lower() for r in await pool.fetch("SELECT title FROM service_rules WHERE vehicle_id=$1 AND deleted_at IS NULL", vid)}
    added = []
    for title, km, months in STANDARD[v["kind"]]:
        if title.lower() in have:
            continue
        await pool.execute("INSERT INTO service_rules (vehicle_id, title, every_km, every_months, last_km, last_date)"
                           " VALUES ($1,$2,$3,$4,$5,$6)", vid, title, km, months, v["mileage"] if km else None, today())
        added.append(title)
    text = (f"📋 Регламент для {esc(v['name'])}: " + ", ".join(esc(a) for a in added) + ".\n"
            "Отсчёт идёт от сегодняшнего дня и текущего пробега. Если что-то делали раньше, напишите, например: "
            "<code>менял масло в мото на 21000</code>.") if added else "Регламент уже добавлен."
    return await garage_reply(user, text)


async def set_mileage(user: User, name: str | None, km) -> Reply:
    v = await find(user.family_id, name)
    k = num(km)
    if not v:
        return Reply(text=_which(await vehicles(user.family_id), "пробег", "23500"))
    if not k or k < 0:
        return Reply(text="Укажите пробег числом: <code>пробег мото 23500</code>")
    k = int(k)
    warn = ""
    if v["mileage"] and k < v["mileage"]:
        warn = f"\n⚠️ Меньше прошлого значения ({v['mileage']:,} км). Если ошиблись — /history.".replace(",", " ")
    pool = db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("INSERT INTO odometer (vehicle_id, km, user_id) VALUES ($1,$2,$3)", v["id"], k, user.id)
        await conn.execute("UPDATE vehicles SET mileage=$2, mileage_at=now() WHERE id=$1", v["id"], k)
    text = f"{KIND_EMOJI[v['kind']]} {esc(v['name'])}: пробег {k:,} км".replace(",", " ") + warn
    due = [r for r in await rules(v["id"]) if r["state"] != "ok"]
    if due:
        text += "\n\n" + "\n".join(rule_line(r) for r in due[:4])
    return Reply(text=text, buttons=[[Button(text=f"✅ {r['title'][:28]}", action=f"o:{r['id']}:done")] for r in due[:3]])


def _which(vs: list[dict], what: str, example: str) -> str:
    if not vs:
        return "В гараже пока пусто. Добавьте: <code>добавь мотоцикл Honda CB400, пробег 21000</code>"
    return (f"Для какой техники {what}? Есть: " + ", ".join(esc(v["name"]) for v in vs)
            + f".\nНапример: <code>{what} {esc(vs[0]['name'])} {example}</code>")


async def rules(vid: int) -> list[dict]:
    pool = db.get_pool()
    v = await pool.fetchrow("SELECT mileage FROM vehicles WHERE id=$1", vid)
    rows = await pool.fetch("SELECT * FROM service_rules WHERE vehicle_id=$1 AND deleted_at IS NULL ORDER BY id", vid)
    out = []
    d = today()
    for r in rows:
        r = dict(r)
        km_left = days_left = None
        if r["every_km"] and r["last_km"] is not None and v["mileage"] is not None:
            km_left = r["last_km"] + r["every_km"] - v["mileage"]
        if r["every_months"] and r["last_date"]:
            days_left = (add_months(r["last_date"], r["every_months"]) - d).days
        r["km_left"], r["days_left"] = km_left, days_left
        km_warn = max(300, (r["every_km"] or 0) // 20)
        if (km_left is not None and km_left <= 0) or (days_left is not None and days_left <= 0):
            r["state"] = "due"
        elif (km_left is not None and km_left <= km_warn) or (days_left is not None and days_left <= 14):
            r["state"] = "soon"
        else:
            r["state"] = "ok"
        out.append(r)
    return out


def rule_line(r: dict) -> str:
    icon = {"due": "🔴", "soon": "🟡", "ok": "🟢"}[r["state"]]
    parts = []
    if r["km_left"] is not None:
        parts.append(f"через {r['km_left']:,} км".replace(",", " ") if r["km_left"] > 0 else
                     f"просрочено на {-r['km_left']:,} км".replace(",", " "))
    if r["days_left"] is not None:
        parts.append(f"через {r['days_left']} дн." if r["days_left"] > 0 else f"просрочено на {-r['days_left']} дн.")
    if not parts:
        parts.append("нет данных о прошлом разе")
    return f"{icon} {esc(r['title'])} — {', '.join(parts)}"


async def set_rule(user: User, data: dict) -> Reply:
    v = await find(user.family_id, data.get("vehicle"))
    if not v:
        return Reply(text=_which(await vehicles(user.family_id), "регламент", "масло каждые 6000 км"))
    title = str(data.get("title") or "").strip()[:80]
    every_km, every_m = num(data.get("every_km")), num(data.get("every_months"))
    if not title or not (every_km or every_m):
        return Reply(text="Пример: <code>масло мото каждые 6000 км или раз в год</code>")
    r = await _find_rule(v["id"], title)
    pool = db.get_pool()
    last_km = num(data.get("last_km"))
    last_date = parse_date(data.get("last_date"))
    if r:
        await pool.execute("UPDATE service_rules SET every_km=$2, every_months=$3, last_km=coalesce($4,last_km),"
                           " last_date=coalesce($5,last_date) WHERE id=$1", r["id"], int(every_km) if every_km else None,
                           int(every_m) if every_m else None, int(last_km) if last_km else None, last_date)
    else:
        await pool.execute("INSERT INTO service_rules (vehicle_id, title, every_km, every_months, last_km, last_date)"
                           " VALUES ($1,$2,$3,$4,$5,$6)", v["id"], title, int(every_km) if every_km else None,
                           int(every_m) if every_m else None,
                           int(last_km) if last_km else (v["mileage"] if every_km else None), last_date or today())
    return await garage_reply(user, f"📋 Регламент «{esc(title)}» для {esc(v['name'])} сохранён.")


async def _find_rule(vid: int, title: str) -> dict | None:
    rows = await db.get_pool().fetch("SELECT * FROM service_rules WHERE vehicle_id=$1 AND deleted_at IS NULL", vid)
    t = title.lower().replace("ё", "е")
    for r in rows:
        if r["title"].lower() == t:
            return dict(r)
    stems = [w[:max(4, len(w) - 2)] for w in t.split() if len(w) >= 4]
    best = None
    for r in rows:
        score = sum(s in r["title"].lower().replace("ё", "е") for s in stems)
        if score and (best is None or score > best[0]):
            best = (score, dict(r))
    return best[1] if best else None


async def service_done(user: User, data: dict) -> Reply:
    v = await find(user.family_id, data.get("vehicle"))
    if not v:
        return Reply(text=_which(await vehicles(user.family_id), "обслуживание", "менял масло на 21000"))
    title = str(data.get("title") or "Обслуживание").strip()[:120]
    km = num(data.get("km"))
    km = int(km) if km else v["mileage"]
    done_on = parse_date(data.get("date")) or today()
    cost = num(data.get("cost"))
    rule = await _find_rule(v["id"], title)
    return await _log(user, v, rule, title if not rule else rule["title"], km, done_on, cost)


async def _log(user: User, v: dict, rule: dict | None, title: str, km: int | None, done_on: date, cost) -> Reply:
    tx_id = None
    if cost:
        rows = await finance.save(user, [{"kind": "expense", "amount": cost, "category": "Авто",
                                          "merchant": None, "note": f"{v['name']}: {title}", "date": done_on.isoformat(),
                                          "vehicle_id": v["id"]}], source="garage")
        tx_id = rows[0]["id"] if rows else None
    pool = db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("INSERT INTO service_log (vehicle_id, rule_id, title, km, done_on, cost, transaction_id, user_id,"
                           " prev_km, prev_date) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)", v["id"], rule["id"] if rule else None,
                           title, km, done_on, cost, tx_id, user.id, rule["last_km"] if rule else None,
                           rule["last_date"] if rule else None)
        if rule:
            await conn.execute("UPDATE service_rules SET last_km=coalesce($2,last_km), last_date=$3, reminded_at=NULL,"
                               " snooze_until=NULL WHERE id=$1", rule["id"], km, done_on)
        if km and (v["mileage"] is None or km > v["mileage"]):
            await conn.execute("UPDATE vehicles SET mileage=$2, mileage_at=now() WHERE id=$1", v["id"], km)
            await conn.execute("INSERT INTO odometer (vehicle_id, km, user_id) VALUES ($1,$2,$3)", v["id"], km, user.id)
        await db.emit(conn, "garage.service", {"vehicle": v["name"], "title": title, "km": km, "cost": cost})
    text = f"🔧 {esc(v['name'])}: {esc(title)}" + (f" на {km:,} км".replace(",", " ") if km else "") + f", {done_on:%d.%m.%Y}"
    if cost:
        text += f"\n💸 Записал трату {finance.money(cost)} (Авто)"
    if rule and (rule["every_km"] or rule["every_months"]):
        nxt = []
        if rule["every_km"] and km:
            nxt.append(f"на {km + rule['every_km']:,} км".replace(",", " "))
        if rule["every_months"]:
            nxt.append(f"до {add_months(done_on, rule['every_months']):%d.%m.%Y}")
        text += "\nСледующий раз: " + " или ".join(nxt)
    return Reply(text=text, buttons=[[Button(text="🏍 Гараж", action="o:0:list")]])


async def fuel_per_100(vid: int, days: int = 120) -> Decimal | None:
    """₽ на 100 км: траты на топливо по этой технике между первым и последним замером пробега."""
    pool = db.get_pool()
    od = await pool.fetch("SELECT km, created_at FROM odometer WHERE vehicle_id=$1 AND deleted_at IS NULL"
                          " AND created_at > now() - make_interval(days => $2) ORDER BY created_at", vid, days)
    if len(od) < 2 or od[-1]["km"] - od[0]["km"] < 100:
        return None
    spent = await pool.fetchval(
        "SELECT coalesce(sum(t.amount),0) FROM transactions t JOIN categories c ON c.id=t.category_id"
        " WHERE t.vehicle_id=$1 AND t.deleted_at IS NULL AND c.name='Топливо' AND t.created_at > $2 AND t.created_at <= $3",
        vid, od[0]["created_at"], od[-1]["created_at"])
    if not spent:
        return None
    return (Decimal(str(spent)) * 100 / (od[-1]["km"] - od[0]["km"])).quantize(Decimal("1"))


async def garage_reply(user: User, head: str = "") -> Reply:
    from . import papers
    vs = await vehicles(user.family_id)
    if not vs:
        return Reply(text=(head + "\n\n" if head else "") + "🏍 В гараже пусто.\nДобавьте: "
                     "<code>добавь мотоцикл Honda CB400, пробег 21000</code> или <code>добавь машину Toyota RAV4</code>")
    parts, buttons = [], []
    for v in vs:
        s = f"{KIND_EMOJI[v['kind']]} <b>{esc(v['name'])}</b>"
        if v["mileage"] is not None:
            s += f" — {v['mileage']:,} км".replace(",", " ")
            if v["mileage_at"]:
                s += f" (на {v['mileage_at'].astimezone(get_settings().zone):%d.%m})"
        rs = await rules(v["id"])
        if rs:
            s += "\n" + "\n".join(rule_line(r) for r in sorted(rs, key=lambda r: ["due", "soon", "ok"].index(r["state"]))[:6])
            for r in rs:
                if r["state"] != "ok":
                    buttons.append([Button(text=f"✅ {v['name'][:12]}: {r['title'][:22]}", action=f"o:{r['id']}:done")])
        else:
            buttons.append([Button(text=f"📋 Регламент ТО: {v['name'][:20]}", action=f"o:{v['id']}:std")])
        for p in await papers.for_vehicle(user.family_id, v["id"]):
            s += "\n" + papers.short_line(p)
        f = await fuel_per_100(v["id"])
        if f:
            s += f"\n⛽ Топливо: ≈ {finance.money(f)} на 100 км"
        parts.append(s)
    text = (head + "\n\n" if head else "") + "\n\n".join(parts) + \
        "\n\nПробег: <code>пробег мото 23500</code> · ТО: <code>менял масло в мото на 23500, 4500 р</code>"
    return Reply(text=text, buttons=buttons[:8])


async def handle_action(user: User, parts: list[str]) -> Reply:
    op = parts[2]
    if op == "list":
        return await garage_reply(user)
    obj = int(parts[1])
    if op == "std":
        return await add_standard(user, obj)
    pool = db.get_pool()
    rule = await pool.fetchrow("SELECT r.*, v.family_id FROM service_rules r JOIN vehicles v ON v.id=r.vehicle_id"
                               " WHERE r.id=$1 AND r.deleted_at IS NULL", obj)
    if not rule or rule["family_id"] != user.family_id:
        return Reply(text="Пункт регламента не найден.")
    if op == "done":
        v = next(x for x in await vehicles(user.family_id) if x["id"] == rule["vehicle_id"])
        return await _log(user, v, dict(rule), rule["title"], v["mileage"], today(), None)
    if op == "later":
        await pool.execute("UPDATE service_rules SET snooze_until=$2 WHERE id=$1", obj, today() + timedelta(days=7))
        return Reply(text=f"⏰ Напомню про «{esc(rule['title'])}» через неделю.")
    return Reply(text="Неизвестное действие.")


# ---------- Напоминания ----------

async def send_due() -> None:
    n = now()
    if n.hour < get_settings().daily_reminder_hour:
        return
    d = n.date()
    pool = db.get_pool()
    for v in await pool.fetch("SELECT * FROM vehicles WHERE deleted_at IS NULL"):
        v = dict(v)
        # ТО: не чаще раза в неделю на пункт
        for r in await rules(v["id"]):
            if r["state"] == "ok" or (r["snooze_until"] and r["snooze_until"] > d):
                continue
            if r["reminded_at"] and r["reminded_at"] > n - timedelta(days=7):
                continue
            await pool.execute("UPDATE service_rules SET reminded_at=now() WHERE id=$1", r["id"])
            head = "🔴 <b>Пора на ТО</b>" if r["state"] == "due" else "🟡 <b>Скоро ТО</b>"
            await notify.remind_about("garage", v["family_id"], v["user_id"], Reply(
                text=f"{head}\n{KIND_EMOJI[v['kind']]} {esc(v['name'])}: {rule_line(r)}",
                buttons=[[Button(text="✅ Сделал", action=f"o:{r['id']}:done"),
                          Button(text="⏰ Через неделю", action=f"o:{r['id']}:later")]]))
        # Пробег: раз в месяц спросить, если есть пункты по километрам (мотоцикл — только в сезон)
        has_km = await pool.fetchval("SELECT 1 FROM service_rules WHERE vehicle_id=$1 AND every_km IS NOT NULL"
                                     " AND deleted_at IS NULL LIMIT 1", v["id"])
        in_season = v["kind"] == "car" or 4 <= d.month <= 10
        stale = v["mileage_at"] is None or v["mileage_at"] < n - timedelta(days=30)
        asked = v["asked_at"] and v["asked_at"] > n - timedelta(days=30)
        if has_km and in_season and stale and not asked:
            await pool.execute("UPDATE vehicles SET asked_at=now() WHERE id=$1", v["id"])
            await notify.remind_about("garage", v["family_id"], v["user_id"], Reply(
                text=f"{KIND_EMOJI[v['kind']]} Какой сейчас пробег у {esc(v['name'])}? Напишите: "
                     f"<code>пробег {esc(v['name'])} {(v['mileage'] or 0) + 1000}</code>\nПо нему считаю, когда пора на ТО."))
        # Сезон
        for key, kind, (mo, day), text in SEASON:
            if kind != v["kind"] or d < date(d.year, mo, day) or d > date(d.year, mo, day) + timedelta(days=10):
                continue
            ok = await pool.fetchval("INSERT INTO season_sent (vehicle_id, key, year) VALUES ($1,$2,$3)"
                                     " ON CONFLICT DO NOTHING RETURNING year", v["id"], key, d.year)
            if ok:
                await notify.remind_about("garage", v["family_id"], v["user_id"], Reply(text=f"{text}\n\n{KIND_EMOJI[v['kind']]} {esc(v['name'])}"))


async def morning_lines(user: User) -> list[str]:
    out = []
    for v in await vehicles(user.family_id):
        for r in await rules(v["id"]):
            if r["state"] == "due":
                out.append(f"🔧 {esc(v['name'])}: {esc(r['title'])} — пора")
    return out[:3]


async def recalc_mileage(conn, vid: int) -> None:
    """После отмены или возврата записи пробега — пересчитать текущий пробег."""
    r = await conn.fetchrow("SELECT km, created_at FROM odometer WHERE vehicle_id=$1 AND deleted_at IS NULL"
                            " ORDER BY created_at DESC, id DESC LIMIT 1", vid)
    await conn.execute("UPDATE vehicles SET mileage=$2, mileage_at=$3 WHERE id=$1", vid,
                       r["km"] if r else None, r["created_at"] if r else None)
