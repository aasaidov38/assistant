"""Веб-панель, версия 1.5: платежи, выписки, накопления, гараж, документы, счётчики, заметки, лекарства.

Все адреса — под /web/api/, только для вошедших через бота и только данные своей семьи
(та же проверка, что и в web.py: current_user).
"""
import re
from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from . import alice, cards, push, release, ticktick
from . import bank, bills, db, debts, finance, garage, habits, meds, meters, notes, nutrition, papers, prefs, savings, scope, trips, users
from .config import get_settings
from .schemas import Reply
from .util import today
from .web import _member, _mirror, current_user

router = APIRouter()
MAX_UPLOAD = 15 * 1024 * 1024


def plain(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def jsonable(v):
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items() if k != "data"}
    if isinstance(v, list):
        return [jsonable(x) for x in v]
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if hasattr(v, "strftime"):          # time
        return v.strftime("%H:%M")
    return v


def reply_json(r: Reply) -> dict:
    return {"text": plain(r.text)}


@router.get("/web/api/categories")
async def categories(user: users.User = Depends(current_user)):
    return await finance.categories()


# ---------- 1. Регулярные платежи ----------

class BillIn(BaseModel):
    title: str
    amount: float | None = None
    day: int
    period_months: int = 1
    category: str | None = None
    next_date: str | None = None
    remind_days: int = 3


@router.get("/web/api/bills")
async def bills_list(user: users.User = Depends(current_user)):
    rows = await bills.all_bills(user.family_id)
    for r in rows:
        r["icon"], r["days_left"] = bills.status(r)
    left, _ = bills.month_totals(rows)
    return {"bills": jsonable(rows), "left": float(left), "paid": float(await bills.paid_this_month(user.family_id))}


@router.post("/web/api/bills")
async def bill_create(body: BillIn, user: users.User = Depends(current_user)):
    r = await bills.create(user, body.model_dump())
    return reply_json(r)


class PayIn(BaseModel):
    amount: float | None = None


@router.post("/web/api/bills/{bill_id}/pay")
async def bill_pay(bill_id: int, body: PayIn, user: users.User = Depends(current_user)):
    b, text = await bills.pay(user, bill_id, body.amount)
    if b:
        await _mirror(user, text)
    return {"ok": bool(b), "text": plain(text)}


@router.post("/web/api/bills/{bill_id}/skip")
async def bill_skip(bill_id: int, user: users.User = Depends(current_user)):
    b, text = await bills.pay(user, bill_id, skip=True)
    return {"ok": bool(b), "text": plain(text)}


@router.post("/web/api/bills/{bill_id}/delete")
async def bill_delete(bill_id: int, user: users.User = Depends(current_user)):
    return {"ok": await bills.delete(user, bill_id)}


# ---------- 2. Выписки ----------

@router.post("/web/api/bank/upload")
async def bank_upload(file: UploadFile = File(...), user: users.User = Depends(current_user)):
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Файл больше 15 МБ")
    imp, r = await bank.import_file(user, data, file.filename or "", file.content_type or "")
    if not imp:
        raise HTTPException(400, plain(r.text))
    return {"id": imp, "text": plain(r.text)}


@router.get("/web/api/bank")
async def bank_recent(user: users.User = Depends(current_user)):
    rows = await db.get_pool().fetch(
        "SELECT i.id, i.filename, i.bank, i.date_from, i.date_to, i.created_at,"
        " count(r.*) FILTER (WHERE r.status='new' AND NOT r.is_transfer) AS new"
        " FROM bank_imports i LEFT JOIN bank_rows r ON r.import_id=i.id WHERE i.family_id=$1"
        " GROUP BY i.id ORDER BY i.id DESC LIMIT 10", user.family_id)
    return jsonable([dict(r) for r in rows])


@router.get("/web/api/bank/{imp}")
async def bank_rows(imp: int, user: users.User = Depends(current_user)):
    head, rows = await bank.rows_of(user.family_id, imp)
    if not head:
        raise HTTPException(404, "Выписка не найдена")
    return {"import": jsonable(head), "rows": jsonable(rows)}


class BankPick(BaseModel):
    id: int
    category: str | None = None


class BankAddIn(BaseModel):
    rows: list[BankPick]


@router.post("/web/api/bank/{imp}/add")
async def bank_add(imp: int, body: BankAddIn, user: users.User = Depends(current_user)):
    n = await bank.add_rows(user, imp, [p.model_dump() for p in body.rows])
    if n:
        await _mirror(user, f"🏦 Из выписки добавлено операций: {n}")
    return {"added": n}


@router.post("/web/api/bank/{imp}/skip")
async def bank_skip(imp: int, user: users.User = Depends(current_user)):
    return {"skipped": await bank.skip_rest(user, imp)}


# ---------- 3. Накопления ----------

class GoalIn(BaseModel):
    title: str
    target: float
    deadline: str | None = None


class MoveIn(BaseModel):
    amount: float


@router.get("/web/api/savings")
async def savings_list(user: users.User = Depends(current_user)):
    gs = await savings.goals(user.family_id)
    for g in gs:
        g["per_month"] = savings.per_month(g)
    return jsonable(gs)


@router.post("/web/api/savings")
async def savings_create(body: GoalIn, user: users.User = Depends(current_user)):
    return reply_json(await savings.create(user, body.model_dump()))


@router.post("/web/api/savings/{gid}/move")
async def savings_move(gid: int, body: MoveIn, user: users.User = Depends(current_user)):
    g = next((x for x in await savings.goals(user.family_id) if x["id"] == gid), None)
    if not g:
        raise HTTPException(404, "Цель не найдена")
    r = await savings.move_by_name(user, g["title"], body.amount)
    await _mirror(user, r.text)
    return reply_json(r)


@router.post("/web/api/savings/{gid}/close")
async def savings_close(gid: int, user: users.User = Depends(current_user)):
    await db.get_pool().execute("UPDATE savings_goals SET closed_at=now() WHERE id=$1 AND family_id=$2", gid, user.family_id)
    return {"ok": True}


# ---------- 4. Гараж ----------

class VehicleIn(BaseModel):
    name: str
    kind: str = "car"
    km: int | None = None


class KmIn(BaseModel):
    km: int


class ServiceIn(BaseModel):
    title: str
    km: int | None = None
    cost: float | None = None
    date: str | None = None


class RuleIn(BaseModel):
    title: str
    every_km: int | None = None
    every_months: int | None = None


async def _vehicle(user: users.User, vid: int) -> dict:
    v = next((x for x in await garage.vehicles(user.family_id) if x["id"] == vid), None)
    if not v:
        raise HTTPException(404, "Техника не найдена")
    return v


@router.get("/web/api/garage")
async def garage_data(user: users.User = Depends(current_user)):
    out = []
    pool = db.get_pool()
    for v in await garage.vehicles(user.family_id):
        log = await pool.fetch("SELECT id, title, km, done_on, cost FROM service_log WHERE vehicle_id=$1 AND deleted_at IS NULL"
                               " ORDER BY done_on DESC, id DESC LIMIT 15", v["id"])
        spent = await pool.fetchval("SELECT coalesce(sum(amount),0) FROM transactions WHERE vehicle_id=$1 AND deleted_at IS NULL"
                                    " AND occurred_on > current_date - 365", v["id"])
        odo = await pool.fetch("SELECT km, created_at FROM odometer WHERE vehicle_id=$1 AND deleted_at IS NULL"
                               " AND created_at > now() - interval '400 days' ORDER BY created_at", v["id"])
        out.append({**v, "odometer": [dict(r) for r in odo], "rules": await garage.rules(v["id"]), "papers": await papers.for_vehicle(user.family_id, v["id"]),
                    "log": [dict(r) for r in log], "fuel_100": await garage.fuel_per_100(v["id"]), "spent_year": spent})
    return jsonable(out)


@router.post("/web/api/garage/vehicles")
async def garage_add(body: VehicleIn, user: users.User = Depends(current_user)):
    return reply_json(await garage.add_vehicle(user, body.model_dump()))


@router.post("/web/api/garage/{vid}/odometer")
async def garage_km(vid: int, body: KmIn, user: users.User = Depends(current_user)):
    v = await _vehicle(user, vid)
    return reply_json(await garage.set_mileage(user, v["name"], body.km))


@router.post("/web/api/garage/{vid}/standard")
async def garage_standard(vid: int, user: users.User = Depends(current_user)):
    await _vehicle(user, vid)
    return reply_json(await garage.add_standard(user, vid))


@router.post("/web/api/garage/{vid}/service")
async def garage_service(vid: int, body: ServiceIn, user: users.User = Depends(current_user)):
    v = await _vehicle(user, vid)
    r = await garage.service_done(user, {"vehicle": v["name"], **body.model_dump()})
    await _mirror(user, r.text)
    return reply_json(r)


@router.post("/web/api/garage/{vid}/rules")
async def garage_rule(vid: int, body: RuleIn, user: users.User = Depends(current_user)):
    v = await _vehicle(user, vid)
    return reply_json(await garage.set_rule(user, {"vehicle": v["name"], **body.model_dump()}))


@router.post("/web/api/garage/rules/{rid}/done")
async def garage_rule_done(rid: int, user: users.User = Depends(current_user)):
    r = await garage.handle_action(user, ["o", str(rid), "done"])
    await _mirror(user, r.text)
    return reply_json(r)


@router.post("/web/api/garage/rules/{rid}/delete")
async def garage_rule_delete(rid: int, user: users.User = Depends(current_user)):
    n = await db.get_pool().execute(
        "UPDATE service_rules r SET deleted_at=now() FROM vehicles v WHERE r.id=$1 AND v.id=r.vehicle_id AND v.family_id=$2",
        rid, user.family_id)
    return {"ok": n.endswith("1")}


# ---------- 5. Документы ----------

class PaperIn(BaseModel):
    kind: str = "document"
    title: str
    owner: str | None = None
    vehicle: str | None = None
    expires_on: str | None = None
    purchased_on: str | None = None
    warranty_months: int | None = None
    note: str | None = None


@router.get("/web/api/papers")
async def papers_list(user: users.User = Depends(current_user)):
    ps = await papers.list_all(user.family_id)
    for p in ps:
        p["days_left"] = papers.days_left(p)
    return jsonable(ps)


@router.post("/web/api/papers")
async def papers_add(body: PaperIn, user: users.User = Depends(current_user)):
    return reply_json(await papers.save(user, body.model_dump()))


@router.get("/web/api/files/{fid}")
async def file_get(fid: int, user: users.User = Depends(current_user)):
    r = await db.get_pool().fetchrow("SELECT mime, data FROM attachments WHERE id=$1 AND family_id=$2", fid, user.family_id)
    if not r:
        raise HTTPException(404, "Файл не найден")
    return Response(content=bytes(r["data"]), media_type=r["mime"],
                    headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})


# ---------- 6. Счётчики ----------

class MeterIn(BaseModel):
    title: str
    kind: str = "other"
    verify_until: str | None = None


class ReadingIn(BaseModel):
    value: float


@router.get("/web/api/meters")
async def meters_list(user: users.User = Depends(current_user)):
    ms = await meters.meters(user.family_id)
    for m in ms:
        m["monthly"] = await meters.monthly(m["id"], 12)
        m["emoji"] = meters.KINDS.get(m["kind"], meters.KINDS["other"])[0]
        m["readings"] = [dict(r) for r in await db.get_pool().fetch(
            "SELECT id, value, created_at FROM meter_readings WHERE meter_id=$1 AND deleted_at IS NULL"
            " ORDER BY created_at DESC LIMIT 6", m["id"])]
    miss = {m["id"] for m in await meters.missing(user.family_id)}
    for m in ms:
        m["missing"] = m["id"] in miss
    return jsonable(ms)


@router.post("/web/api/meters")
async def meters_add(body: MeterIn, user: users.User = Depends(current_user)):
    return reply_json(await meters.add(user, body.model_dump()))


@router.post("/web/api/meters/{mid}/reading")
async def meters_reading(mid: int, body: ReadingIn, user: users.User = Depends(current_user)):
    m = next((x for x in await meters.meters(user.family_id) if x["id"] == mid), None)
    if not m:
        raise HTTPException(404, "Счётчик не найден")
    return reply_json(await meters.reading(user, [{"meter": m["title"], "value": body.value}]))


# ---------- 7. Заметки ----------

class NoteIn(BaseModel):
    subject: str | None = None
    text: str


@router.get("/web/api/notes")
async def notes_list(q: str = "", user: users.User = Depends(current_user)):
    ns = await notes.all_notes(user.family_id, 500)
    q = q.lower().replace("ё", "е").strip()
    if q:
        stems = [w[:max(3, len(w) - 2)] for w in q.split() if len(w) >= 3]
        ns = [n for n in ns if all(s in f"{n['subject'] or ''} {n['text']}".lower().replace("ё", "е") for s in stems)]
    return jsonable(ns)


@router.post("/web/api/notes")
async def notes_add(body: NoteIn, user: users.User = Depends(current_user)):
    r = await notes.save(user, body.subject, body.text)
    if r.text.startswith("🔒"):
        raise HTTPException(400, plain(r.text))
    return reply_json(r)


# ---------- 8. Лекарства ----------

class MedIn(BaseModel):
    title: str
    dose: str | None = None
    times: list[str]
    for_whom: str | None = None
    stock: float | None = None
    per_intake: float | None = 1
    unit: str | None = "шт"
    end_on: str | None = None
    user_id: int | None = None       # кому напоминать (по умолчанию — себе)


@router.get("/web/api/meds")
async def meds_list(user_id: int | None = None, user: users.User = Depends(current_user)):
    m = await _member(user, user_id, "meds")
    ms = await meds.meds(user.family_id, m.id)
    for x in ms:
        x["days_of_stock"] = meds.days_of_stock(x)
        x["schedule"] = meds.schedule_text(x)
    stats = await db.get_pool().fetch(
        "SELECT m.id, count(*) FILTER (WHERE i.status='taken') AS taken, count(*) AS total FROM med_intakes i"
        " JOIN meds m ON m.id=i.med_id WHERE m.user_id=$1 AND i.due_at > now() - interval '14 days' GROUP BY m.id", m.id)
    st = {r["id"]: {"taken": r["taken"], "total": r["total"]} for r in stats}
    for x in ms:
        x["adherence"] = st.get(x["id"])
    return {"meds": jsonable(ms), "today": jsonable(await meds.today_status(user.family_id, m.id))}


@router.post("/web/api/meds")
async def meds_add(body: MedIn, user: users.User = Depends(current_user)):
    owner = await _member(user, body.user_id, "meds")
    return reply_json(await meds.add(owner, body.model_dump()))


@router.post("/web/api/meds/intakes/{iid}/{op}")
async def meds_intake(iid: int, op: str, user: users.User = Depends(current_user)):
    if op not in ("take", "skip", "untake"):
        raise HTTPException(400, "Неизвестное действие")
    return reply_json(await meds.handle_action(user, ["x", f"i{iid}", op]))


@router.post("/web/api/meds/{mid}/take")
async def meds_take_now(mid: int, user: users.User = Depends(current_user)):
    m = next((x for x in await meds.meds(user.family_id) if x["id"] == mid), None)
    if not m:
        raise HTTPException(404, "Лекарство не найдено")
    owner = await users.get(m["user_id"])
    return reply_json(await meds.take_by_name(owner, m["title"]))


@router.post("/web/api/meds/{mid}/stop")
async def meds_stop(mid: int, user: users.User = Depends(current_user)):
    return reply_json(await meds.handle_action(user, ["x", f"m{mid}", "stopok"]))


# ---------- 13. Поездки ----------

class TripIn(BaseModel):
    title: str
    kind: str = "car"
    start_on: str
    end_on: str | None = None
    budget: float | None = None
    route: str | None = None          # «Иркутск — Листвянка — Ольхон»


class ItemIn(BaseModel):
    title: str


async def _trip(user: users.User, tid: int) -> dict:
    t = next((x for x in await trips.trips(user.family_id, True) if x["id"] == tid), None)
    if not t:
        raise HTTPException(404, "Поездка не найдена")
    return t


@router.get("/web/api/trips")
async def trips_list(user: users.User = Depends(current_user)):
    out = []
    pool = db.get_pool()
    for t in await trips.trips(user.family_id, True):
        daily = await pool.fetch("SELECT occurred_on AS d, sum(amount) AS s FROM transactions WHERE trip_id=$1"
                                 " AND deleted_at IS NULL AND kind='expense' GROUP BY 1 ORDER BY 1", t["id"])
        out.append({**t, "places": await trips.places(t["id"]), "checklist": await trips.checklist(t["id"]),
                    "categories": await trips.by_category(t["id"]), "daily": [dict(r) for r in daily],
                    "status": trips._status(t)})
    return jsonable(out)


@router.get("/web/api/trips/{tid}/weather")
async def trips_weather(tid: int, user: users.User = Depends(current_user)):
    return jsonable(await trips.weather(await _trip(user, tid)))


@router.post("/web/api/trips")
async def trips_create(body: TripIn, user: users.User = Depends(current_user)):
    d = body.model_dump()
    d["route"] = [x.strip() for x in re.split(r"\s+-\s+|[—–>,;→]+", body.route or "") if x.strip()]
    r = await trips.create(user, d)
    return reply_json(r)


@router.post("/web/api/trips/{tid}/items")
async def trips_item_add(tid: int, body: ItemIn, user: users.User = Depends(current_user)):
    await _trip(user, tid)
    await db.get_pool().execute("INSERT INTO trip_checklist (trip_id, title, sort) VALUES ($1,$2,200)", tid, body.title.strip()[:100])
    return {"ok": True}


@router.post("/web/api/trips/items/{iid}/toggle")
async def trips_item_toggle(iid: int, user: users.User = Depends(current_user)):
    r = await trips.toggle(user, iid)
    if not r:
        raise HTTPException(404, "Пункт не найден")
    return r


@router.post("/web/api/trips/{tid}/delete")
async def trips_delete(tid: int, user: users.User = Depends(current_user)):
    return {"ok": await trips.delete(user, tid)}


# ---------- Сводка для главной ----------

@router.get("/web/api/dashboard")
async def dashboard(user: users.User = Depends(current_user)):
    pool = db.get_pool()
    fc, fct = scope.cond("finance", "user_id"), scope.cond("finance", "t.user_id")
    d = today()
    m0 = d.replace(day=1)
    pm0 = (m0 - timedelta(days=1)).replace(day=1)
    daily = await pool.fetch(f"SELECT occurred_on AS d, sum(amount) AS s FROM transactions WHERE family_id=$1 AND deleted_at IS NULL AND {fc}"
                             " AND kind='expense' AND occurred_on > $2 GROUP BY 1 ORDER BY 1", user.family_id, d - timedelta(days=30))
    by_day = {r["d"]: float(r["s"]) for r in daily}
    spend30 = [{"date": (d - timedelta(days=29 - i)).isoformat(), "sum": by_day.get(d - timedelta(days=29 - i), 0.0)} for i in range(30)]
    month = await pool.fetchval(f"SELECT coalesce(sum(amount),0) FROM transactions WHERE family_id=$1 AND deleted_at IS NULL AND {fc}"
                                " AND kind='expense' AND occurred_on >= $2 AND occurred_on <= $3", user.family_id, m0, d)
    prev_same = await pool.fetchval(f"SELECT coalesce(sum(amount),0) FROM transactions WHERE family_id=$1 AND deleted_at IS NULL AND {fc}"
                                    " AND kind='expense' AND occurred_on >= $2 AND occurred_on <= $3", user.family_id, pm0,
                                    min(pm0 + timedelta(days=d.day - 1), m0 - timedelta(days=1)))
    income = await pool.fetchval(f"SELECT coalesce(sum(amount),0) FROM transactions WHERE family_id=$1 AND deleted_at IS NULL AND {fc}"
                                 " AND kind='income' AND occurred_on >= $2", user.family_id, m0)
    split = await pool.fetch("SELECT c.name, c.emoji, sum(t.amount) AS s FROM transactions t JOIN categories c ON c.id=t.category_id"
                             f" WHERE t.family_id=$1 AND t.deleted_at IS NULL AND t.kind='expense' AND t.occurred_on >= $2 AND {fct}"
                             " GROUP BY 1,2 ORDER BY 3 DESC", user.family_id, m0)
    top = [dict(r) for r in split[:5]]
    rest = sum(float(r["s"]) for r in split[5:])
    if rest:
        top.append({"name": "Другое", "emoji": "", "s": rest})
    kcal = []
    for i in range(13, -1, -1):
        day = d - timedelta(days=i)
        tot = await nutrition.day_totals(user, day)
        kcal.append({"date": day.isoformat(), "kcal": float(tot["kcal"] or 0)})
    upcoming = []
    for b in await bills.all_bills(user.family_id):
        _, left = bills.status(b)
        if left <= 7:
            upcoming.append({"type": "bill", "icon": "💳", "title": b["title"], "date": b["next_due"], "days": left,
                             "value": float(b["amount"]) if b["amount"] else None})
    for p in await papers.list_all(user.family_id):
        left = papers.days_left(p)
        if left is not None and left <= 30:
            upcoming.append({"type": "paper", "icon": "📄", "title": p["title"] + (f" ({p['owner']})" if p["owner"] else ""),
                             "date": p["expires_on"], "days": left, "value": None})
    for v in await garage.vehicles(user.family_id):
        for r in await garage.rules(v["id"]):
            if r["state"] != "ok":
                upcoming.append({"type": "garage", "icon": "🔧", "title": f"{v['name']}: {r['title']}", "date": None,
                                 "days": 0 if r["state"] == "due" else 7, "value": None})
    miss = await meters.missing(user.family_id)
    ms = await meters.meters(user.family_id)
    if miss and ms and d.day >= min(m["submit_from"] for m in ms):
        upcoming.append({"type": "meter", "icon": "🔢", "title": f"Показания счётчиков: нет {len(miss)} из {len(ms)}",
                         "date": None, "days": max(0, min(m["submit_to"] for m in ms) - d.day), "value": None})
    ds = await debts.debts(user.family_id)
    for x in ds:
        if x["due_on"] and x["user_id"] == user.id and (x["due_on"] - d).days <= 7:
            upcoming.append({"type": "debt", "icon": "🤝", "title": x["person"] + (": вернуть" if x["direction"] == "borrowed" else ": ждём возврат"), "date": x["due_on"], "days": (x["due_on"] - d).days, "value": float(x["balance"])})
    upcoming.sort(key=lambda x: x["days"])
    gs = await savings.goals(user.family_id)
    trip = await trips.current(user.family_id)
    meds_today = await meds.today_status(user.family_id, user.id)
    return jsonable({
        "spend30": spend30, "month": month, "prev_same": prev_same, "income": income, "split": top,
        "kcal14": kcal, "kcal_goal": nutrition.goal_for(user), "upcoming": upcoming[:8],
        "savings": {"saved": sum(float(g["saved"]) for g in gs), "target": sum(float(g["target"]) for g in gs), "goals": len(gs)},
        "trip": {**trip, "status": trips._status(trip)} if trip else None,
        "meds": meds_today,
        "debts": {k: float(v) for k, v in debts.totals(ds).items()},
        "personal": {k: scope.is_personal(k) for k in scope.SECTIONS},
    })


# ---------- 1.7. Долги ----------

class DebtIn(BaseModel):
    direction: str
    person: str
    amount: float
    due: str | None = None
    note: str | None = None


class DebtMoveIn(BaseModel):
    amount: float | None = None


async def _debt_moves(ids: list[int]) -> dict[int, list]:
    if not ids:
        return {}
    rows = await db.get_pool().fetch(
        "SELECT m.id, m.debt_id, m.kind, m.amount, m.occurred_on, m.note, u.name AS user_name FROM debt_moves m"
        " LEFT JOIN users u ON u.id=m.user_id WHERE m.debt_id = ANY($1::bigint[]) AND m.deleted_at IS NULL"
        " ORDER BY m.occurred_on, m.id", ids)
    out: dict[int, list] = {}
    for r in rows:
        out.setdefault(r["debt_id"], []).append({**dict(r), "ref": f"p.{r['id']}"})
    return out


@router.get("/web/api/debts")
async def debts_list(closed: int = 0, user: users.User = Depends(current_user)):
    ds = await debts.debts(user.family_id, include_closed=bool(closed))
    moves = await _debt_moves([d["id"] for d in ds])
    for d in ds:
        d["ref"] = f"e.{d['id']}"
        d["moves"] = moves.get(d["id"], [])
        d["mine"] = d["user_id"] == user.id
    open_ = [d for d in ds if not d["closed"]]
    return jsonable({"debts": ds, "totals": debts.totals(open_), "personal": scope.is_personal("debts"),
                     "overdue": sum(1 for d in open_ if d["due_on"] and d["due_on"] < today())})


@router.post("/web/api/debts")
async def debts_add(body: DebtIn, user: users.User = Depends(current_user)):
    r = await debts.create(user, body.model_dump())
    await _mirror(user, r.text)
    return reply_json(r)


async def _own_debt(user: users.User, did: int) -> dict:
    d = await debts.get(user, did)
    if not d:
        raise HTTPException(404, "Долг не найден")
    return d


@router.post("/web/api/debts/{did}/repay")
async def debts_repay(did: int, body: DebtMoveIn, user: users.User = Depends(current_user)):
    d = await _own_debt(user, did)
    if d["closed"]:
        raise HTTPException(400, "Долг уже закрыт")
    a = Decimal(str(body.amount)) if body.amount and body.amount > 0 else d["balance"]
    a = min(a, d["balance"])
    await debts._move(user, d, "repay", a)
    d = await debts.get(user, did)
    text = ("✅ Долг закрыт: " if d["closed"] else f"💸 Вернули {finance.money(a)}: ") + debts.line(d)
    await _mirror(user, text)
    return {"text": plain(text)}


@router.post("/web/api/debts/{did}/add")
async def debts_more(did: int, body: DebtMoveIn, user: users.User = Depends(current_user)):
    d = await _own_debt(user, did)
    if not body.amount or body.amount <= 0:
        raise HTTPException(400, "Укажите сумму")
    await debts._move(user, d, "add", Decimal(str(body.amount)))
    d = await debts.get(user, did)
    text = f"➕ Добавили к долгу {finance.money(body.amount)}: " + debts.line(d)
    await _mirror(user, text)
    return {"text": plain(text)}


@router.post("/web/api/debts/{did}/forgive")
async def debts_forgive(did: int, user: users.User = Depends(current_user)):
    await _own_debt(user, did)
    r = await debts.forgive(user, did)
    await _mirror(user, r.text)
    return reply_json(r)


@router.post("/web/api/debts/{did}/due")
async def debts_due(did: int, body: dict, user: users.User = Depends(current_user)):
    await _own_debt(user, did)
    from .util import parse_date
    due = parse_date(body.get("due"))
    await db.get_pool().execute("UPDATE debts SET due_on=$2, reminded='{}', snooze_until=NULL WHERE id=$1", did, due)
    return {"text": f"Срок: {due:%d.%m.%Y}" if due else "Срок убран"}


# ---------- 1.8. Карты лояльности ----------

class CardIn(BaseModel):
    store: str
    number: str
    format: str | None = None
    note: str | None = None


@router.get("/web/api/cards")
async def cards_list(user: users.User = Depends(current_user)):
    cs = [{**c, "ref": f"c.{c['id']}", "mine": c["user_id"] == user.id} for c in await cards.cards(user.family_id)]
    return jsonable({"cards": cs, "personal": scope.is_personal("cards"),
                     "formats": cards.FORMAT_NAMES})


@router.post("/web/api/cards")
async def cards_add(body: CardIn, user: users.User = Depends(current_user)):
    r = await cards.add(user, body.model_dump(), source="web")
    ok = r.text.startswith("💳 Сохранил")
    if ok:
        await _mirror(user, r.text)
    return {"ok": ok, "text": r.text}


@router.post("/web/api/cards/{cid}/used")
async def cards_used(cid: int, user: users.User = Depends(current_user)):
    if not await cards.cards(user.family_id, cid):
        raise HTTPException(404, "Карты нет")
    await cards.mark_used(user, cid)
    return {"ok": True}


# ---------- 1.8. Интеграции и push ----------

SOON = [("❤️ Здоровье: шаги, сон, пульс", "Health Connect на Android (Mi Fitness и др.)", "1.9"),
        ("🏦 Уведомления банков", "Траты из push и SMS банков на телефоне", "1.9"),
        ("📶 Баланс мобильного", "Остаток и списания оператора", "1.9"),
        ("🍏 iPhone", "Команды для приложения «Быстрые команды»", "1.9"),
        ("✉️ Gmail", "Чеки, счета и брони из почты", "1.10")]


@router.get("/web/api/integrations")
async def integrations(user: users.User = Depends(current_user)):
    import time as _t
    s = get_settings()
    chans = {c for c, _ in await users.identities(user.id)}
    tt = {"configured": ticktick.configured(), "connected": False}
    if tt["configured"]:
        info = await ticktick.token_info(user.id)
        tt.update(connected=info["connected"], expired=bool(info.get("expires_at")) and not info["connected"],
                  notify=info.get("notify", True), last_error=info.get("last_error"),
                  days=int((info["expires_at"] - _t.time()) // 86400) if info.get("expires_at") else None,
                  sync_min=int((_t.time() - info["last_sync"]) // 60) if info.get("last_sync") else None,
                  link=ticktick.auth_link(user.id))
    keys = await push.vapid() if push.AVAILABLE else None
    site = s.public_url.rstrip("/") if s.public_url else ""
    return jsonable({
        "telegram": {"connected": "telegram" in chans},
        "ticktick": tt,
        "alice": {"configured": bool(s.alice_secret and s.public_url), "linked": "alice" in chans},
        "push": {"available": bool(keys), "key": keys["pub"] if keys else None, "count": await push.count(user.id)},
        "android": {"url": site + "/#apps" if site else None},
        "soon": [{"title": a, "sub": b, "version": v} for a, b, v in SOON],
    })


@router.post("/web/api/integrations/ticktick/disconnect")
async def ticktick_disconnect(user: users.User = Depends(current_user)):
    await db.get_pool().execute("DELETE FROM integrations WHERE user_id=$1 AND provider='ticktick'", user.id)
    return {"text": "TickTick отключён. Задачи в нём остались, ассистент их больше не видит."}


@router.post("/web/api/integrations/alice/code")
async def alice_code(user: users.User = Depends(current_user)):
    return {"text": (await alice.link_code(user)).text}


@router.post("/web/api/push/subscribe")
async def push_subscribe(body: dict, request: Request, user: users.User = Depends(current_user)):
    if not push.AVAILABLE:
        raise HTTPException(503, "Push недоступен на сервере")
    try:
        await push.subscribe(user.id, body, request.headers.get("user-agent", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "count": await push.count(user.id)}


@router.post("/web/api/push/unsubscribe")
async def push_unsubscribe(body: dict, user: users.User = Depends(current_user)):
    await push.unsubscribe(user.id, str(body.get("endpoint") or ""))
    return {"ok": True, "count": await push.count(user.id)}


@router.post("/web/api/push/test")
async def push_test(user: users.User = Depends(current_user)):
    n = await push.send(user.id, {"title": "🔔 Проверка уведомлений", "body": "Так будут выглядеть напоминания ассистента.",
                                  "url": "/web/#set"})
    return {"ok": n > 0, "text": f"Отправил на устройств: {n}" if n else "Не дошло ни до одного устройства"}


# ---------- 1.7. Настройки ----------

@router.get("/web/api/settings")
async def settings_get(user: users.User = Depends(current_user)):
    members = await users.family_members(user.family_id)
    return jsonable({
        "me": await prefs.full(user),
        "owner": bool(members) and members[0].id == user.id,
        "owner_name": members[0].name if members else "",
        "modes": await scope.modes(user.family_id),
        "sections": {k: {"title": v[0], "default": v[1]} for k, v in scope.SECTIONS.items()},
        "topics": prefs.TOPICS, "tabs": prefs.TABS,
        "goals": {k: {"title": v[0], "unit": v[1]} for k, v in prefs.GOALS.items()},
        "system": await release.status(bool(members) and members[0].id == user.id),
    })


@router.post("/web/api/update")
async def update_start(body: dict, user: users.User = Depends(current_user)):
    r = await release.request_update(user, str(body.get("version") or ""))
    return {"text": r.text}


@router.post("/web/api/settings")
async def settings_save(body: dict, user: users.User = Depends(current_user)):
    return jsonable(await prefs.update(user, body))


@router.post("/web/api/sections")
async def sections_save(body: dict, user: users.User = Depends(current_user)):
    try:
        modes = await scope.set_modes(user, body)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    return modes
