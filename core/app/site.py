"""Публичный сайт проекта (версия 1.7.2): https://assistant.saidov.ru/

Статические файлы лежат в static/site: index.html и картинки в img/.
Адреса: «/» — страница, «/site/…» — её файлы. Данных семьи здесь нет: скриншоты сделаны на демо-данных.
Чтобы сайт открывался снаружи, в Caddy к @public добавлены пути «/» и «/site/*» (docs/DEPLOY_CLAUDE_CODE.md).
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter()
SITE = (Path(__file__).resolve().parent / "static" / "site").resolve()
TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".svg": "image/svg+xml",
         ".css": "text/css", ".js": "text/javascript", ".ico": "image/x-icon"}


@router.get("/", include_in_schema=False)
async def site_index():
    return FileResponse(SITE / "index.html", media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "public, max-age=600"})


@router.get("/site/{path:path}", include_in_schema=False)
async def site_file(path: str):
    f = (SITE / path).resolve()
    # только файлы внутри static/site и только известных типов
    if SITE not in f.parents or not f.is_file() or f.suffix.lower() not in TYPES:
        raise HTTPException(404, "Нет такого файла")
    return FileResponse(f, media_type=TYPES[f.suffix.lower()], headers={"Cache-Control": "public, max-age=86400"})
