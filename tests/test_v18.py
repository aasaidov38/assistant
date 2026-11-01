"""Версия 1.8: уведомления о новой версии, обновление по кнопке, статус копий — на настоящей PostgreSQL."""
import _env  # noqa: F401  (пути и база)
import asyncio, json, os, subprocess, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import asyncpg
from app import ai, brain, db, notify, release, scope, users, web_life
from app.schemas import Action

from _env import PSQL
sql = lambda s: subprocess.run(PSQL, input=s, capture_output=True, text=True, check=True).stdout.strip()
FAILS, SENT = [], []


def check(cond, name, info=""):
    print(("  ✔ " if cond else "  ✖ ") + name + ("" if cond else f"  → {str(info)[:500]}"))
    if not cond:
        FAILS.append(name)


async def fake_deliver(uid, reply):
    SENT.append((uid, reply.text, [b.action for r in reply.buttons for b in r if b.action]))
    return True


SEED = """
TRUNCATE families, users, user_identities, outbox, app_state RESTART IDENTITY CASCADE;
INSERT INTO families (name) VALUES ('Демо');
INSERT INTO users (family_id, name) VALUES (1,'Алексей'), (1,'Мария');
INSERT INTO user_identities (channel, external_id, user_id) VALUES ('telegram','111',1),('telegram','222',2);
"""

CHANGELOG = """# История
## 1.9 — 10.12.2026
- Новая штука
- Вторая <важная>

## 1.8 — 01.11.2026
- Карты лояльности
- Голос удержанием

## 1.7.2 — 01.11.2026
- Сайт
"""

iso = lambda dt: dt.isoformat()
utc = lambda: datetime.now(timezone.utc)


def put(name, data):
    (release.STATE / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


async def press(uid, action):
    return await brain.handle_action(Action(external_user_id=str(uid), action=action))


async def main():
    db.pool = asyncpg.FakeConn()
    sql(SEED)
    notify.deliver = fake_deliver
    tmp = Path(tempfile.mkdtemp())
    release.STATE = tmp
    alex, maria = await users.get(1), await users.get(2)

    print("1. Список изменений")
    check(release.notes("1.9", CHANGELOG) == ["Новая штука", "Вторая <важная>"], "заметки 1.9", release.notes("1.9", CHANGELOG))
    check(release.notes("1.7.2", CHANGELOG) == ["Сайт"], "заметки 1.7.2 (три цифры)")
    check(release.notes("2.0", CHANGELOG) == [], "нет версии — пусто")
    check(release.notes(release.VERSION), "в образе есть CHANGELOG с текущей версией", release.CHANGELOG)
    check(release.newer("1.10", "1.9") and not release.newer("1.8", "1.8") and release.newer("1.7.2", "1.7"),
          "сравнение версий 1.10 > 1.9, 1.7.2 > 1.7")
    root = Path(_env.HERE).parent
    check((root / "VERSION").read_text().strip() == release.VERSION, "файл VERSION совпадает с release.VERSION")

    print("2. Объявление после обновления")
    SENT.clear(); await release.announce_if_updated()
    check(not SENT, "первая установка — без объявления", SENT)
    sql("UPDATE app_state SET value='{\"version\":\"1.7.2\"}' WHERE key='announced'")
    SENT.clear(); await release.announce_if_updated()
    check(len(SENT) == 2 and "обновлён до версии " + release.VERSION in SENT[0][1], "после обновления — всем в семье", SENT)
    SENT.clear(); await release.announce_if_updated()
    check(not SENT, "второй запуск той же версии — тишина", SENT)
    sql("UPDATE app_state SET value='{\"version\":\"9.0\"}' WHERE key='announced'")
    SENT.clear(); await release.announce_if_updated()
    check(not SENT, "откат на старую версию — тишина", SENT)

    print("3. Предложение обновиться")
    SENT.clear(); await release.check()
    check(not SENT, "нет файла от агента — ничего", SENT)
    put("update-available.json", {"version": release.VERSION, "notes": []})
    SENT.clear(); await release.check()
    check(not SENT, "та же версия — ничего", SENT)
    put("update-available.json", {"version": "9.9", "notes": ["Новая штука", "<b>x</b>"]})
    SENT.clear(); await release.check()
    check(len(SENT) == 1 and SENT[0][0] == 1 and "9.9" in SENT[0][1] and "&lt;b&gt;" in SENT[0][1]
          and "y:9.9:go" in SENT[0][2] and "y:9.9:later" in SENT[0][2], "владельцу: что нового и две кнопки", SENT)
    SENT.clear(); await release.check()
    check(not SENT, "повторно сразу — тишина", SENT)
    st = json.loads(sql("select value from app_state where key='update_offer'"))
    st["next_at"] = iso(utc() - timedelta(minutes=1))
    sql(f"UPDATE app_state SET value='{json.dumps(st)}' WHERE key='update_offer'")
    SENT.clear(); await release.check()
    check(len(SENT) == 1 and "Напоминаю" in SENT[0][1], "через 3 дня — напоминание", SENT)

    print("4. Кнопки")
    r = await press(222, "y:9.9:go")
    check("только владелец" in r.text, "Мария не может обновить", r.text)
    r = await press(111, "y:9.9:go")
    check("Агент обновлений" in r.text and not (tmp / "update-request.json").exists(), "агент молчит — честно говорим", r.text)
    put("host.json", {"at": iso(utc()), "version": release.VERSION,
                      "local_backup": {"at": iso(utc() - timedelta(hours=3)), "size_mb": 4.2},
                      "offsite": {"ok_at": iso(utc() - timedelta(hours=40))}, "disk": {"free_gb": 21.5}})
    r = await press(111, "y:9.9:later")
    check("напомню через 3" in r.text, "«Напомнить позже»", r.text)
    r = await press(111, "y:9.9:go")
    req = json.loads((tmp / "update-request.json").read_text()) if (tmp / "update-request.json").exists() else {}
    check("Обновляю до 9.9" in r.text and req.get("version") == "9.9", "владелец — команда агенту записана", (r.text, req))
    r = await press(111, "y:9.9:go")
    check("уже запущено" in r.text, "двойное нажатие", r.text)
    r = await press(111, "y:5.0:go")
    check("не актуальна" in r.text, "кнопка со старой версией", r.text)
    SENT.clear(); await release.check()
    check(not SENT, "после запроса напоминаний нет", SENT)

    print("5. Итог обновления")
    (tmp / "update-request.json").unlink()
    put("update-result.json", {"version": "9.9", "from": release.VERSION, "ok": False,
                               "message": "Новая версия не запустилась", "at": iso(utc())})
    SENT.clear(); await release.check()
    check(len(SENT) == 1 and "не получилось" in SENT[0][1] and "не запустилась" in SENT[0][1], "неудача — владельцу", SENT)
    SENT.clear(); await release.check()
    check(not SENT, "о той же неудаче — один раз", SENT)
    st = json.loads(sql("select value from app_state where key='update_offer'"))
    check(not st.get("requested") and st.get("next_at"), "после неудачи снова напомним позже", st)
    put("update-result.json", {"version": "9.9", "from": release.VERSION, "ok": True, "message": "ok", "at": iso(utc() + timedelta(seconds=1))})
    SENT.clear(); await release.check()
    check(not SENT, "удача — объявит сама новая версия при старте", SENT)

    print("6. /version")
    r = await brain.handle_text(alex, "/version", source="text")
    check(f"Версия ассистента: {release.VERSION}" in r.text and "Доступна 9.9" in r.text and "4.2 МБ" in r.text
          and "давно" in r.text and "21.5 ГБ" in r.text, "/version: версия, обновление, копии, диск", r.text)
    check(any(b.action == "y:9.9:go" for row in r.buttons for b in row), "у владельца — кнопка обновления")
    r = await brain.handle_text(maria, "/version", source="text")
    check(not r.buttons, "у Марии кнопки нет", r.buttons)
    put("host.json", {"at": iso(utc() - timedelta(hours=1))})
    r = await brain.handle_text(alex, "/version", source="text")
    check("Агент на сервере не отвечает" in r.text and "не настроено" in r.text, "агент давно молчал", r.text)

    print("7. Панель: настройки")
    put("host.json", {"at": iso(utc()), "local_backup": {"at": iso(utc())}, "disk": {"free_gb": 20}})
    await scope.enter(alex); s = (await web_life.settings_get(user=alex))["system"]
    check(s["version"] == release.VERSION and s["available"]["version"] == "9.9" and s["can_update"], "владелец видит обновление", s)
    r = await web_life.update_start({"version": "9.9"}, user=alex)
    check("Обновляю" in r["text"] and (tmp / "update-request.json").exists(), "кнопка в панели запускает обновление", r)
    await scope.enter(maria); s = (await web_life.settings_get(user=maria))["system"]
    check(not s["can_update"] and s["updating"], "Мария: без кнопки, видит «идёт обновление»", s)
    scope.leave()

    print("8. Голос из браузера")
    check(ai.audio_name("audio/webm;codecs=opus") == ("voice.webm", "audio/webm"), "webm из Chrome", ai.audio_name("audio/webm;codecs=opus"))
    check(ai.audio_name("audio/mp4")[0] == "voice.m4a" and ai.audio_name(None)[0] == "voice.ogg" and ai.audio_name("x/y")[0] == "voice.ogg",
          "Safari mp4 → m4a, по умолчанию ogg")
    html = (root / "core/app/static/chat.html").read_text()
    check("MediaRecorder" in html and "pointerdown" in html and "setPointerCapture" in html, "чат: удержание и запись в браузере")

    print("ИТОГ:", "всё прошло" if not FAILS else FAILS)


asyncio.run(main())
