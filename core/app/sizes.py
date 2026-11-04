"""Размеры (версия 1.10.3): сколько места занимает база и копии в облаке, и предупреждения владельцу.

База: pg_database_size(), лимит по умолчанию 10 ГБ (место на сервере). Фото отдельно: сумма размеров attachments.
Облако: размер репозитория restic в Яндексе считает агент на сервере раз в час и кладёт в state/host.json
(offsite.size_mb); лимит 1 ГБ (бесплатный бакет). Это размер самих данных restic; старые версии объектов бакета,
которые Яндекс ещё не стёр по правилу жизненного цикла, сверху не учтены, поэтому порог предупреждения 70%, а не 95%.
Предупреждения: при 70% (обычное) и 90% (срочное), каждое повторяется раз в 7 дней, пока уровень не упал.
"""
import logging
import os
import time

from . import db, notify, release
from .schemas import Reply
from .util import esc

log = logging.getLogger("sizes")

DB_LIMIT_MB = float(os.environ.get("DB_LIMIT_GB", "10")) * 1024
CLOUD_LIMIT_MB = float(os.environ.get("CLOUD_LIMIT_MB", "1024"))
ALERT = "sizes:alert"
REPEAT = 7 * 24 * 3600
WARN, CRIT = 0.70, 0.90
_last = 0.0
CHECK_EVERY = 3600


def level(used: float | None, limit: float) -> str:
    if used is None or limit <= 0:
        return "unknown"
    r = used / limit
    return "crit" if r >= CRIT else "warn" if r >= WARN else "ok"


def fmt(mb: float | None) -> str:
    if mb is None:
        return "нет данных"
    return f"{mb / 1024:.1f} ГБ".replace(".", ",") if mb >= 1024 else f"{mb:.0f} МБ" if mb >= 10 else f"{mb:.1f} МБ".replace(".", ",")


async def measure() -> dict:
    pool = db.get_pool()
    total = await pool.fetchval("SELECT pg_database_size(current_database())") or 0
    att = await pool.fetchrow("SELECT count(*) AS n, COALESCE(sum(octet_length(data)),0) AS b FROM attachments")
    h = release.host()
    off = h.get("offsite") or {}
    cloud = off.get("size_mb")
    disk = h.get("disk") or {}
    return {
        "db": {"mb": round(total / 1048576, 1), "limit_mb": DB_LIMIT_MB, "level": level(total / 1048576, DB_LIMIT_MB)},
        "photos": {"mb": round(att["b"] / 1048576, 1), "count": att["n"]},
        "cloud": {"mb": cloud, "limit_mb": CLOUD_LIMIT_MB, "level": level(cloud, CLOUD_LIMIT_MB), "at": off.get("size_at")},
        "disk": disk,
    }


def _pct(mb, limit) -> int:
    return round(mb * 100 / limit) if mb is not None and limit else 0


def report_text(m: dict) -> str:
    d, c, p = m["db"], m["cloud"], m["photos"]
    lines = ["🗄 <b>Место</b>",
             f"• База: {fmt(d['mb'])} из {fmt(d['limit_mb'])} ({_pct(d['mb'], d['limit_mb'])}%)",
             f"• Фото и файлы в базе: {fmt(p['mb'])} ({p['count']} шт.)",
             f"• Копии в Яндекс Облаке: {fmt(c['mb'])} из {fmt(c['limit_mb'])}" + (f" ({_pct(c['mb'], c['limit_mb'])}%)" if c["mb"] is not None else "")]
    if m["disk"]:
        lines.append(f"• Свободно на диске сервера: {m['disk'].get('free_gb')} ГБ")
    return "\n".join(lines)


async def check_alerts(m: dict | None = None) -> int:
    """Пишет владельцу при 70% и 90% базы или облачных копий. Возвращает число отправленных сообщений."""
    m = m or await measure()
    st = await release.get_state(ALERT)
    now = time.time()
    sent = 0
    for kind, title, tip in (
            ("db", "База ассистента", "Сожмите старые фото командой /shrink и удалите ненужные записи."),
            ("cloud", "Копии в Яндекс Облаке", "Бесплатный лимит хранилища 1 ГБ. Уменьшите число хранимых копий или очистите бакет.")):
        item = m[kind]
        lv = item["level"]
        prev = st.get(kind) or {}
        if lv in ("ok", "unknown"):
            if prev.get("level") != lv:
                st[kind] = {"level": lv, "at": prev.get("at", 0)}
            continue
        if prev.get("level") == lv and now - prev.get("at", 0) < REPEAT:
            continue
        text = (f"{'🚨' if lv == 'crit' else '⚠️'} <b>{esc(title)}: заполнено {_pct(item['mb'], item['limit_mb'])}%</b>\n"
                f"Занято {esc(fmt(item['mb']))} из {esc(fmt(item['limit_mb']))}.\n{esc(tip)}")
        for o in await release._owner():
            ok = await notify.send(o.id, Reply(text=text), topic="sizes", urgent=(lv == "crit"))
            sent += 1 if ok else 0
        st[kind] = {"level": lv, "at": now}
    await release.set_state(ALERT, st)
    return sent


async def check_due() -> None:
    """Фоновая задача (jobs.loop): проверять раз в час."""
    global _last
    if time.time() - _last < CHECK_EVERY:
        return
    _last = time.time()
    await check_alerts()
