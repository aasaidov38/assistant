"""Версия 1.10.3: сжатие фото и лимит файла, размеры базы и облачных копий с предупреждениями, установка приложения — на PostgreSQL."""
import os
import _env  # noqa: F401  (пути и база)
import asyncio, base64, io, subprocess
import asyncpg
from pathlib import Path
from app import brain, db, media, notify, papers, prefs, release, sizes, users, web
from app.config import get_settings

from _env import PSQL
sql = lambda s: subprocess.run(PSQL, input=s, capture_output=True, text=True, check=True).stdout.strip()
FAILS, SENT = [], []
REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "core" / "app" / "static"
try:
    from PIL import Image          # в GitHub Actions Pillow нет: проверки сжатия пропускаются
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


def check(cond, name, info=""):
    print(("  ✔ " if cond else "  ✖ ") + name + ("" if cond else f"  → {str(info)[:500]}"))
    if not cond:
        FAILS.append(name)


SEED = """
TRUNCATE families, users, user_identities, app_state, outbox, attachments RESTART IDENTITY CASCADE;
INSERT INTO families (name) VALUES ('Демо');
INSERT INTO users (family_id, name) VALUES (1,'Алексей'), (1,'Мария');
INSERT INTO user_identities (channel, external_id, user_id) VALUES ('telegram','111',1),('telegram','222',2);
"""


async def fake_send(uid, reply, topic=None, urgent=False, quiet="defer"):
    SENT.append((uid, reply.text, topic, urgent))
    return True


def big_photo(w=3200, h=2400) -> bytes:
    img = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))     # шум плохо жмётся: как фото с телефона
    out = io.BytesIO()
    img.save(out, "JPEG", quality=95)
    return out.getvalue()


async def main():
    db.pool = asyncpg.FakeConn()
    sql(SEED)
    notify.send = fake_send
    cfg = get_settings()
    alex, maria = await users.get(1), await users.get(2)

    print("1. Сжатие картинок")
    check(media.compress(b"x" * 500_000, "application/pdf") == (b"x" * 500_000, "application/pdf"), "не картинка — не трогаем")
    check(media.compress(b"abc", "image/jpeg") == (b"abc", "image/jpeg"), "маленький файл — не трогаем")
    check(media.compress(b"\x00" * 400_000, "image/jpeg")[0] == b"\x00" * 400_000, "битая картинка — остаётся как есть")
    check(media.too_big(b"x" * (15 * 1024 * 1024 + 1)) and not media.too_big(b"x" * 1000), "лимит 15 МБ")
    if HAVE_PIL:
        src = big_photo()
        out, mime = media.compress(src, "image/jpeg")
        im = Image.open(io.BytesIO(out))
        check(mime == "image/jpeg" and len(out) < len(src) * 0.6, f"большое фото стало меньше ({len(src) // 1024} → {len(out) // 1024} КБ)")
        check(max(im.size) == media.MAX_SIDE, "длинная сторона 1600 px", im.size)
        png = io.BytesIO(); Image.new("RGBA", (2400, 1800), (200, 30, 30, 120)).save(png, "PNG")
        o2, m2 = media.compress(png.getvalue() + b"", "image/png") if len(png.getvalue()) >= media.SKIP_BELOW else (png.getvalue(), "image/png")
        check(m2 in ("image/png", "image/jpeg"), "PNG с прозрачностью не падает")
    else:
        print("  (Pillow не установлен — проверки сжатия пропущены)")

    print("2. Сохранение вложений и разовое сжатие")
    if HAVE_PIL:
        src = big_photo()
        aid = await papers.save_attachment(1, src, "image/jpeg")
        stored = int(sql(f"SELECT octet_length(data) FROM attachments WHERE id={aid}"))
        check(stored < len(src) * 0.6, "в базу попало сжатое фото", (stored, len(src)))
        raw_id = sql(f"INSERT INTO attachments (family_id, mime, data) VALUES (1,'image/jpeg', decode('{src[:1].hex()}','hex')) RETURNING id").split()[0]
        sql(f"UPDATE attachments SET data = decode('{src.hex()}','hex') WHERE id={raw_id}")
        r = await media.shrink_old()
        check(r["shrunk"] >= 1 and r["saved_bytes"] > 0, "старое тяжёлое фото сжато разовой командой", r)
        check(int(sql(f"SELECT octet_length(data) FROM attachments WHERE id={raw_id}")) < len(src) * 0.6, "в базе уже лёгкое")
        r2 = await media.shrink_old()
        check(r2["shrunk"] == 0, "повторный запуск ничего не меняет", r2)
    r = await brain.handle_photo(alex, base64.b64encode(b"x" * (16 * 1024 * 1024)).decode(), "image/jpeg", "")
    check("слишком большой" in r.text, "фото больше 15 МБ отклоняется до обращения к ИИ", r.text)

    print("3. Размеры и уровни")
    check([sizes.level(x, 100) for x in (10, 69.9, 70, 89.9, 90, 120)] == ["ok", "ok", "warn", "warn", "crit", "crit"], "уровни 70% и 90%")
    check(sizes.level(None, 100) == "unknown", "нет данных — unknown")
    check(sizes.fmt(1536) == "1,5 ГБ" and sizes.fmt(5.8) == "5,8 МБ" and sizes.fmt(None) == "нет данных", "красивые числа", (sizes.fmt(1536), sizes.fmt(5.8)))
    host = {"disk": {"free_gb": 19.0, "used_pct": 34}, "offsite": {"size_mb": 23.2, "size_at": "2026-11-04T10:00:00+00:00"}}
    release.host = lambda: dict(host)
    sql("INSERT INTO attachments (family_id, mime, data) VALUES (1,'image/jpeg', decode('ffd8ff','hex'))")
    m = await sizes.measure()
    check(m["db"]["mb"] > 0 and m["db"]["level"] == "ok" and m["cloud"]["mb"] == 23.2 and m["cloud"]["level"] == "ok", "замер: база и облако", m)
    check(m["photos"]["count"] >= 1, "фото посчитаны", m["photos"])
    txt = sizes.report_text(m)
    check("База:" in txt and "Яндекс" in txt and "19.0 ГБ" in txt, "текст отчёта", txt)

    print("4. Предупреждения владельцу")
    SENT.clear(); sql("TRUNCATE app_state")
    check(await sizes.check_alerts() == 0 and not SENT, "всё в порядке — тихо", SENT)
    host["offsite"]["size_mb"] = 800
    await sizes.check_alerts()
    check(len(SENT) == 1 and SENT[0][0] == 1 and SENT[0][2] == "sizes" and SENT[0][3] is False and "78%" in SENT[0][1], "облако 78% — одно обычное сообщение владельцу", SENT)
    await sizes.check_alerts()
    check(len(SENT) == 1, "повтор сразу не шлём", SENT)
    host["offsite"]["size_mb"] = 950
    await sizes.check_alerts()
    check(len(SENT) == 2 and SENT[1][3] is True and "93%" in SENT[1][1], "93% — срочное сообщение", SENT)
    host["offsite"]["size_mb"] = 100
    await sizes.check_alerts()
    st = await release.get_state(sizes.ALERT)
    check(st["cloud"]["level"] == "ok", "после очистки уровень сброшен", st)
    host["offsite"]["size_mb"] = 800
    await sizes.check_alerts()
    check(len(SENT) == 3, "снова 70% — снова сообщение", SENT)
    check("sizes" in prefs.TOPICS, "тема уведомлений «sizes» есть в настройках")

    print("5. Команды бота")
    cfg.public_url = "https://assistant.example.ru"
    r = await brain.handle_text(alex, "/app", "telegram")
    url = r.buttons[0][0].url if r.buttons else ""
    check("Safari" in r.text and "Chrome" in r.text and "Telegram" in r.text and url.startswith("https://assistant.example.ru/web/login?t="), "/app: инструкция и одноразовая ссылка", r.text[:200])
    cfg.public_url = ""
    r = await brain.handle_text(alex, "/app", "telegram")
    check("PUBLIC_URL" in r.text and not r.buttons, "/app без публичного адреса — подсказка")
    r = await brain.handle_text(alex, "/size", "telegram")
    check("База:" in r.text, "/size: владельцу отчёт", r.text)
    r = await brain.handle_text(maria, "/size", "telegram")
    check("только владельцу" in r.text, "/size: Марии отказ", r.text)
    r = await brain.handle_text(maria, "/shrink", "telegram")
    check("только владелец" in r.text, "/shrink: Марии отказ", r.text)
    r = await brain.handle_text(alex, "/shrink", "telegram")
    check("🗜" in r.text, "/shrink: владельцу ответ", r.text)
    r = await release.version_reply(alex)
    check("Размер копий в облаке" in r.text, "/version показывает размер облака", r.text)

    print("6. Панель, бот, агент, зависимости")
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    for needle in ('id="homeInstall"', "function paintInstall", "function installCase", "inst_hide", "inst_later", 'id="sSizeCard"', "function renderSizeSet", "api('sizes')"):
        check(needle in html, f"в панели: {needle}")
    check("paintInstall();\n" in html and "'appinstalled'" in html, "баннер обновляется по событию установки")
    check('command="app"' in (REPO / "adapters/telegram/bot.py").read_text(encoding="utf-8"), "команда /app в меню Telegram")
    agent = (REPO / "scripts/host-agent.sh").read_text(encoding="utf-8")
    check("restic stats --mode raw-data" in agent and "size_mb" in agent and ".cloud-size" in agent, "агент считает размер облака раз в час")
    check("pillow" in (REPO / "core/requirements.txt").read_text(encoding="utf-8").lower(), "Pillow в зависимостях ядра")
    web_src = (REPO / "core/app/web.py").read_text(encoding="utf-8")
    check('"/web/api/sizes"' in web_src and '"/web/api/sizes/shrink"' in web_src, "маршруты размеров есть")
    check('"Cache-Control": "no-cache"})   # 1.10.3' in web_src and web.MANIFEST["icons"][0]["src"].endswith("?v=3"),
          "манифест не кэшируется, значки с новой версией адреса (?v=3)")

    print("ИТОГ:", "всё прошло" if not FAILS else f"ошибок {len(FAILS)}: {FAILS}")


asyncio.run(main())
