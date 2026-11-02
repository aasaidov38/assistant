"""Публичный сайт проекта (с версии 1.7.2, обновлён в 1.9): https://assistant.saidov.ru/

Статические файлы лежат в static/site: index.html и картинки в img/.
Адреса: «/» — страница, «/site/…» — её файлы. Данных семьи здесь нет: скриншоты сделаны на демо-данных.
Чтобы сайт открывался снаружи, в Caddy к @public добавлены пути «/» и «/site/*» (docs/DEPLOY_CLAUDE_CODE.md).

С версии 1.9.1 здесь же лежит Android-приложение: /site/Assistant.apk (static/site/Assistant.apk, номер сборки —
в static/site/APK_VERSION). Скачивается без входа на GitHub. Как обновить — docs/RELEASING.md.
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter()
SITE = (Path(__file__).resolve().parent / "static" / "site").resolve()
TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".svg": "image/svg+xml",
         ".css": "text/css", ".js": "text/javascript", ".ico": "image/x-icon",
         ".apk": "application/vnd.android.package-archive"}
APK_VERSION = SITE / "APK_VERSION"


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
    if f.suffix.lower() == ".apk":
        # имя файла с номером сборки, чтобы на телефоне было видно, какая версия скачана;
        # кэш короткий: адрес один и тот же, а файл меняется с каждой новой сборкой
        ver = APK_VERSION.read_text().strip() if APK_VERSION.is_file() else ""
        name = f"{f.stem}-{ver}.apk" if ver else f.name
        return FileResponse(f, media_type=TYPES[".apk"], filename=name, headers={"Cache-Control": "public, max-age=600"})
    return FileResponse(f, media_type=TYPES[f.suffix.lower()], headers={"Cache-Control": "public, max-age=86400"})
