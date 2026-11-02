"""Здоровье с телефона (версия 1.9): шаги, сон, вес и пульс из Health Connect (туда пишет Mi Fitness).

Телефон присылает данные за последние дни целиком при каждой синхронизации — повторная отправка не задваивает:
у каждой записи есть ext_id (шаги и пульс — один на день, сон — по началу сна, вес — по времени взвешивания).
Записи, которые человек отменил в панели, обратно не появляются.
"""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

from . import db
from .config import get_settings
from .util import now, today

LIMITS = {"steps": (1, 200000), "pulse": (25, 250), "sleep": (0.25, 24), "weight": (20, 350)}


def _ts(v) -> datetime | None:
    try:
        if isinstance(v, (int, float)) or str(v).lstrip("-").isdigit():
            return datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc)
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=get_settings().zone)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _day_at(d: date) -> datetime:
    if d >= today():
        return now()
    return datetime.combine(d, time(23, 59), tzinfo=get_settings().zone)


def _dec(v) -> Decimal | None:
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return None


def normalize(it: dict) -> dict | None:
    """Одна запись телефона → {kind, value, at, ext_id, extra} или None, если что-то не так."""
    kind = it.get("kind")
    if kind not in LIMITS:
        return None
    lo, hi = LIMITS[kind]
    extra = None
    if kind in ("steps", "pulse"):
        try:
            d = date.fromisoformat(str(it.get("day")))
        except ValueError:
            return None
        if not today() - timedelta(days=60) <= d <= today():
            return None
        value, at, ext = _dec(it.get("value")), _day_at(d), f"hc:{kind}:{d.isoformat()}"
        if kind == "pulse":
            extra = {k: int(it[k]) for k in ("min", "max") if str(it.get(k, "")).isdigit()} or None
    elif kind == "sleep":
        a, b = _ts(it.get("start")), _ts(it.get("end"))
        if not a or not b or b <= a:
            return None
        value, at, ext = Decimal(str(round((b - a).total_seconds() / 3600, 2))), b, f"hc:sleep:{int(a.timestamp())}"
    else:
        at = _ts(it.get("at"))
        if not at:
            return None
        value, ext = _dec(it.get("value")), f"hc:weight:{int(at.timestamp())}"
        value = value.quantize(Decimal("0.01")) if value is not None else None
    if value is None or not lo <= value <= hi or at > now() + timedelta(minutes=10) or at < now() - timedelta(days=60):
        return None
    if kind in ("steps", "pulse"):
        value = value.quantize(Decimal(1))
    return {"kind": kind, "value": value, "at": at, "ext_id": ext, "extra": extra}


async def ingest(device: dict, items: list) -> dict:
    pool = db.get_pool()
    stats = {"saved": 0, "skipped": 0}
    for it in (items or [])[:500]:
        r = normalize(it) if isinstance(it, dict) else None
        if not r:
            stats["skipped"] += 1
            continue
        await pool.execute(
            "INSERT INTO metrics (user_id, kind, value, measured_at, source, ext_id, extra)"
            " VALUES ($1,$2,$3,$4,'health',$5,$6::jsonb)"
            " ON CONFLICT (user_id, ext_id) WHERE ext_id IS NOT NULL DO UPDATE"
            " SET value=EXCLUDED.value, measured_at=EXCLUDED.measured_at, extra=EXCLUDED.extra"
            " WHERE metrics.deleted_at IS NULL",
            device["user_id"], r["kind"], r["value"], r["at"], r["ext_id"], r["extra"])
        stats["saved"] += 1
    if stats["saved"]:
        await pool.execute("UPDATE devices SET info = info || jsonb_build_object('health_at', now()::text) WHERE id=$1",
                           device["id"])
    return stats
