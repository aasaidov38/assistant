"""Магнитные бури, пыльца и поиск города (версия 1.10). Источники бесплатные, без ключей.

  • бури — индекс Kp (NOAA SWPC, прогноз на 3 дня, шаг 3 часа). Индекс планетарный: для всех городов одинаковый,
    меняется только местное время. Kp от 5 — магнитная буря (шкала G1–G5);
  • пыльца — Open-Meteo Air Quality (модель CAMS), покрытие только Европа и только в сезон пыления;
    вне покрытия все значения пустые — тогда блок не показываем, чтобы не изображать данные;
  • поиск города — геокодер Open-Meteo.
Свежие значения кэшируются в памяти; если источник не ответил, показываем последние удачные.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from . import net

log = logging.getLogger("envdata")
_cache: dict = {}
TTL = 3 * 3600

KP_LABELS = {0: "спокойно", 1: "спокойно", 2: "спокойно", 3: "спокойно", 4: "слегка неспокойно",
             5: "малая буря (G1)", 6: "умеренная буря (G2)", 7: "сильная буря (G3)", 8: "жёсткая буря (G4)",
             9: "экстремальная буря (G5)"}


def kp_label(kp: float) -> str:
    return KP_LABELS[max(0, min(int(kp + 1e-9), 9))]


def parse_kp(rows: list, offset_s: int = 0, now: datetime | None = None) -> dict:
    """Прогноз NOAA → текущий Kp и максимум по трём местным дням."""
    now = now or datetime.now(timezone.utc)
    pts = []
    for r in rows or []:
        try:
            t = datetime.fromisoformat(r["time_tag"]).replace(tzinfo=timezone.utc)
            pts.append((t, float(r["kp"]), r.get("observed")))
        except (KeyError, TypeError, ValueError):
            continue
    if not pts:
        raise ValueError("NOAA: пустой прогноз")
    pts.sort()
    # блок длится 3 часа: текущий — последний, начавшийся не позже «сейчас»
    cur = next((p for p in reversed(pts) if p[0] <= now), pts[0])
    local = lambda t: t + timedelta(seconds=offset_s)
    today = local(now).date()
    days = []
    for i in range(3):
        d = today + timedelta(days=i)
        vals = [(kp, t) for t, kp, _o in pts if local(t).date() == d and (i > 0 or t + timedelta(hours=3) > now)]
        if not vals:
            continue
        mx, at = max(vals)
        days.append({"date": d.isoformat(), "max": round(mx, 1), "label": kp_label(mx), "storm": mx >= 5,
                     "peak": local(at).strftime("%H:%M")})
    if not days:
        raise ValueError("NOAA: нет данных на ближайшие дни")
    return {"now": {"kp": round(cur[1], 1), "label": kp_label(cur[1]), "observed": cur[2]}, "days": days,
            "storm": any(d["storm"] for d in days)}


# Ориентировочные границы (зёрен в м³): слабая / умеренная / высокая / очень высокая
LEVELS = {"tree": (1, 15, 90, 1500), "grass": (1, 5, 20, 200), "weed": (1, 10, 50, 500)}
LEVEL_NAMES = ["нет", "слабая", "умеренная", "высокая", "очень высокая"]
POLLEN = [("birch_pollen", "берёза", "tree"), ("alder_pollen", "ольха", "tree"), ("olive_pollen", "олива", "tree"),
          ("grass_pollen", "злаковые травы", "grass"), ("mugwort_pollen", "полынь", "weed"),
          ("ragweed_pollen", "амброзия", "weed")]


def pollen_level(group: str, v: float) -> int:
    return sum(1 for b in LEVELS[group] if v >= b)


def parse_pollen(j: dict, offset_s: int = 0, now: datetime | None = None) -> dict:
    """Максимум за ближайшие 24 часа по каждому виду. covered=False — вне покрытия модели (все значения пустые)."""
    h = (j or {}).get("hourly") or {}
    times = h.get("time") or []
    now_l = (now or datetime.now(timezone.utc)) + timedelta(seconds=offset_s)
    start = now_l.strftime("%Y-%m-%dT%H:00")
    i0 = next((i for i, t in enumerate(times) if t >= start), 0)
    covered, items = False, []
    for key, name, grp in POLLEN:
        vals = [v for v in (h.get(key) or [])[i0:i0 + 24] if v is not None]
        if vals:
            covered = True
        mx = max(vals) if vals else 0
        lvl = pollen_level(grp, mx)
        if lvl:
            items.append({"code": key.split("_")[0], "name": name, "value": round(mx), "level": lvl,
                          "label": LEVEL_NAMES[lvl]})
    items.sort(key=lambda x: -x["level"])
    return {"covered": covered, "items": items, "top": items[0]["level"] if items else 0}


async def _cached(key, ttl, fetch):
    c = _cache.get(key)
    if c and time.time() - c[0] < ttl:
        return c[1]
    try:
        data = await fetch()
    except Exception as e:
        log.warning("%s: %s", key if isinstance(key, str) else key[0], e)
        return c[1] if c else None          # источник молчит — последнее удачное значение, если было
    _cache[key] = (time.time(), data)
    return data


async def kp(offset_s: int = 0) -> dict | None:
    rows = await _cached("kp", TTL, lambda: net.get("https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json"))
    try:
        return parse_kp(rows, offset_s) if rows else None
    except Exception as e:
        log.warning("Kp: %s", e)
        return None


async def pollen(lat: float, lon: float, offset_s: int = 0) -> dict | None:
    key = ("pollen", round(lat, 1), round(lon, 1))
    j = await _cached(key, TTL, lambda: net.get("https://air-quality-api.open-meteo.com/v1/air-quality", {
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 2,
        "hourly": ",".join(k for k, _n, _g in POLLEN)}))
    try:
        return parse_pollen(j, offset_s) if j else None
    except Exception as e:
        log.warning("Пыльца: %s", e)
        return None


async def search_city(q: str) -> list[dict]:
    q = (q or "").strip()[:60]
    if len(q) < 2:
        return []
    j = await net.get("https://geocoding-api.open-meteo.com/v1/search", {"name": q, "count": 6, "language": "ru", "format": "json"})
    out = []
    for r in (j or {}).get("results") or []:
        try:
            parts = [r.get("admin1"), r.get("country")]
            out.append({"name": r["name"], "label": ", ".join([r["name"]] + [p for p in parts if p and p != r["name"]]),
                        "lat": round(float(r["latitude"]), 4), "lon": round(float(r["longitude"]), 4),
                        "tz": r.get("timezone") or ""})
        except (KeyError, TypeError, ValueError):
            continue
    return out
