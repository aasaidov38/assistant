"""Фото и файлы (версия 1.10.3): сжатие картинок и лимит размера.

Зачем: фото хранятся в базе (таблица attachments) и попадают в резервные копии. Снимок с телефона — 3–8 МБ,
а для чека, талона или показаний счётчика хватает 1600 пикселей по длинной стороне и JPEG 80% (≈150–400 КБ).
Если Pillow не установлен или файл не читается как картинка — оставляем как есть (ничего не ломаем).
"""
import io
import logging

log = logging.getLogger("media")

MAX_BYTES = 15 * 1024 * 1024      # больше этого фото/файл не принимаем
MAX_SIDE = 1600                   # пикселей по длинной стороне
QUALITY = 80
SKIP_BELOW = 200 * 1024           # маленькие файлы не трогаем


def compress(data: bytes, mime: str = "image/jpeg") -> tuple[bytes, str]:
    """Возвращает (байты, mime). Вернёт исходное, если сжатие не помогло или невозможно."""
    if not data or not (mime or "").lower().startswith("image/") or len(data) < SKIP_BELOW:
        return data, mime
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return data, mime
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)          # учесть поворот с телефона
        if img.mode in ("RGBA", "LA", "P"):         # прозрачность → белый фон
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        if max(img.size) > MAX_SIDE:
            img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=QUALITY, optimize=True)
        res = out.getvalue()
    except Exception as e:                          # битый файл, неизвестный формат
        log.info("Не удалось сжать картинку: %s", e)
        return data, mime
    return (res, "image/jpeg") if len(res) < len(data) * 0.9 else (data, mime)


def too_big(data: bytes) -> bool:
    return len(data) > MAX_BYTES


async def shrink_old(family_id: int | None = None, limit: int = 500) -> dict:
    """Разовое сжатие уже загруженных фото. Возвращает {checked, shrunk, saved_bytes}."""
    from . import db
    pool = db.get_pool()
    rows = await pool.fetch(
        "SELECT id, mime FROM attachments WHERE mime LIKE 'image/%' AND octet_length(data) >= $1"
        + (" AND family_id=$3" if family_id else "") + " ORDER BY id LIMIT $2",
        *([SKIP_BELOW, limit] + ([family_id] if family_id else [])))
    n = saved = 0
    for r in rows:
        row = await pool.fetchrow("SELECT data FROM attachments WHERE id=$1", r["id"])
        d = row["data"]
        old = bytes.fromhex(d[2:]) if isinstance(d, str) and d.startswith("\\x") else bytes(d)   # str — только в тестовой заглушке
        new, mime = compress(old, r["mime"])
        if len(new) < len(old):
            await pool.execute("UPDATE attachments SET data=$2, mime=$3 WHERE id=$1", r["id"], new, mime)
            n += 1
            saved += len(old) - len(new)
    return {"checked": len(rows), "shrunk": n, "saved_bytes": saved}
